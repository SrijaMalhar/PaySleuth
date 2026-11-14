"""The jobs a worker runs, exposed so a scheduler can poke them instead.

Every one is idempotent, which is what lets a host with no background worker
run them on a timer and get the same books.
"""

from datetime import date

from fastapi import APIRouter, Depends, Request

from ..alerts import list_alerts, resolve_alert
from ..charges import apply_late_fees, run_billing
from ..close import list_statements, recent_closes, run_close
from ..outbox import dead_messages, relay_once
from ..reminders import queue_reminders
from .deps import conn, institution, page, today
from .schemas import AlertOut, DeadMessageOut

router = APIRouter(tags=["jobs"])


@router.post("/billing/run")
def billing(as_of: date | None = None, inst=Depends(institution), c=Depends(conn),
            day=Depends(today)):
    on = as_of or day
    billed = run_billing(c, inst, on)
    late = apply_late_fees(c, inst, on)
    return {**billed, "late_fees": len(late)}


@router.post("/reminders/run")
def reminders(inst=Depends(institution), c=Depends(conn), day=Depends(today)):
    return {"queued": queue_reminders(c, inst, day)}


@router.post("/outbox/relay")
def relay(request: Request, inst=Depends(institution), c=Depends(conn)):
    return relay_once(c, inst, request.app.state.send, now=request.app.state.now)


@router.get("/outbox/dead", response_model=list[DeadMessageOut])
def dead(paging=Depends(page), inst=Depends(institution), c=Depends(conn)):
    limit, offset = paging
    return dead_messages(c, inst, limit=limit, offset=offset)


@router.post("/close/run")
def close(day: date | None = None, inst=Depends(institution), c=Depends(conn),
          as_of=Depends(today)):
    return run_close(c, inst, day or as_of)


@router.get("/close")
def closes(paging=Depends(page), inst=Depends(institution), c=Depends(conn)):
    limit, offset = paging
    return recent_closes(c, inst, limit=limit, offset=offset)


@router.get("/statements")
def statements(paging=Depends(page), inst=Depends(institution), c=Depends(conn)):
    limit, offset = paging
    return list_statements(c, inst, limit=limit, offset=offset)


@router.get("/alerts", response_model=list[AlertOut])
def alerts(status: str | None = "open", paging=Depends(page),
           inst=Depends(institution), c=Depends(conn)):
    limit, offset = paging
    return list_alerts(c, inst, status=None if status == "all" else status,
                       limit=limit, offset=offset)


@router.post("/alerts/{alert_id}/resolve", response_model=AlertOut)
def resolve(alert_id: int, inst=Depends(institution), c=Depends(conn)):
    return resolve_alert(c, inst, alert_id)
