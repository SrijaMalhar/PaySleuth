"""The REST API. The only module that knows about HTTP.

Endpoints call the functions the other modules already provide and add no
business logic. They are plain def, not async def: psycopg calls block, and
FastAPI runs plain def endpoints in a thread pool, so a slow query never
blocks the event loop.

Money in and out of the API is always a string such as "2000.50", never a
JSON number, because many clients parse JSON numbers as floats.
"""

import re
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import audit
from .actors import Actor, current_actor
from .alerts import list_alerts, resolve_alert
from .approvals import decide, list_approvals, request_reassign
from .charges import apply_late_fees, run_billing
from .close import list_statements, recent_closes, run_close
from .db import make_pool, migrate
from .evaluation import matching_report
from .errors import Conflict, Forbidden, NotFound, PreconditionFailed, PreconditionRequired
from .importer import import_statement
from .institutions import actor_for_key, add_staff, deactivate_staff, list_staff
from .members import create_member, deactivate_member, get_member, list_members, update_member
from .money import to_paise
from .outbox import dead_messages, relay_once
from .payments import assign_credit, get_credit, record_credit, reverse_credit, review_queue, split_credit
from .periods import IST, period_of, today_ist, utc_now
from .reminders import queue_reminders
from .reports import ageing, collection_report, defaulters, statement
from .statements import Credit, parse_narration

MAX_UPLOAD = 10 * 1024 * 1024


# --- request bodies --------------------------------------------------------------------

class MemberIn(BaseModel):
    name: str
    monthly_fee: str = Field(examples=["2000.00"])
    billing_day: int = Field(ge=1, le=31)
    start_period: str = Field(examples=["2026-06"])
    guardian_name: str | None = None
    phone: str | None = None
    concession_pct: int = Field(0, ge=0, le=100)
    upi_ids: list[str] = []


class FeeChange(BaseModel):
    effective_period: str
    monthly_fee: str


class MemberPatch(BaseModel):
    name: str | None = None
    guardian_name: str | None = None
    phone: str | None = None
    billing_day: int | None = Field(None, ge=1, le=31)
    concession_pct: int | None = Field(None, ge=0, le=100)
    fee_change: FeeChange | None = None
    add_upi_ids: list[str] = []
    remove_upi_ids: list[str] = []


class CreditIn(BaseModel):
    """Either the fields, or just the bank's narration line plus amount and time."""
    amount: str
    received_at: datetime
    narration: str | None = None
    utr: str | None = None
    channel: str = "UPI"
    payer_upi_id: str | None = None
    payer_name: str | None = None
    note: str | None = None


class Part(BaseModel):
    member_id: int
    amount: str


class AssignIn(BaseModel):
    member_id: int
    remember_upi_id: bool = True


class SplitIn(BaseModel):
    parts: list[Part]
    remember_upi_id: bool = True


class ReassignIn(BaseModel):
    parts: list[Part]


class StaffIn(BaseModel):
    name: str
    role: str = Field(examples=["clerk"])


class DecisionIn(BaseModel):
    note: str | None = None


class ReverseIn(BaseModel):
    reason: str | None = None
    reference: str | None = None


def _parts(parts: list[Part]) -> list[tuple[int, int]]:
    return [(p.member_id, to_paise(p.amount)) for p in parts]


# --- ETags -----------------------------------------------------------------------------

def _etag(version: int) -> str:
    return f'"{version}"'


def _version_from(if_match: str | None) -> int:
    if not if_match:
        raise PreconditionRequired("send If-Match with the ETag from GET /members/{id}")
    m = re.fullmatch(r'\s*(?:W/)?"(\d+)"\s*', if_match)
    if not m:
        raise HTTPException(status_code=400, detail='If-Match must be an ETag like "3"')
    return int(m.group(1))


def _with_etag(view: dict, response: Response) -> dict:
    response.headers["ETag"] = _etag(view["version"])
    return view


# --- the app ---------------------------------------------------------------------------

def create_app(dsn: str, send, *, today=today_ist, now=utc_now, pool_options: str | None = None,
               pool_size: int = 20) -> FastAPI:
    """Build the app in a function, so tests can pass a fake sender, a fixed date and a test schema."""
    pool = make_pool(dsn, max_size=pool_size, options=pool_options)
    migrate(pool)

    @asynccontextmanager
    async def lifespan(app):
        yield
        pool.close()

    app = FastAPI(title="PaySleuth", version="1.0",
                  description="Matches UPI bank-statement credits to the members they pay for.",
                  lifespan=lifespan)
    app.state.pool = pool

    def _error(status: int):
        def handler(request: Request, exc: Exception):
            return JSONResponse(status_code=status, content={"detail": str(exc)})
        return handler

    for error, status in ((Forbidden, 403), (NotFound, 404), (Conflict, 409), (PreconditionFailed, 412),
                          (PreconditionRequired, 428), (ValueError, 422)):
        app.add_exception_handler(error, _error(status))

    def conn():
        with pool.connection() as c:
            yield c

    def _identify(key: str) -> Actor | None:
        with pool.connection() as c:
            return actor_for_key(c, key)

    @app.middleware("http")
    async def identify(request: Request, call_next):
        # Look the key up once, here, and set who is acting for everything this
        # request does: the context is copied into the thread that runs the endpoint,
        # so the audit log records the person without every function passing them along.
        key = request.headers.get("x-api-key")
        who = await run_in_threadpool(_identify, key) if key else None
        request.state.actor = who
        token = current_actor.set(who.staff_id if who else None)
        try:
            return await call_next(request)
        finally:
            current_actor.reset(token)

    def actor(request: Request, x_api_key: str | None = Header(None)) -> Actor:
        who = request.state.actor
        if who is None:
            raise HTTPException(status_code=401, detail="missing or unknown X-API-Key")
        return who

    def institution(who: Actor = Depends(actor)) -> int:
        return who.institution_id

    def owner(who: Actor = Depends(actor)) -> Actor:
        if who.role != "owner":
            raise Forbidden("only an owner can do this")
        return who

    @app.get("/health")
    def health(c=Depends(conn)):
        c.execute("SELECT 1")
        return {"status": "ok"}

    # members

    @app.post("/members", status_code=201)
    def add_member(body: MemberIn, response: Response, inst=Depends(institution), c=Depends(conn)):
        view = create_member(c, inst, name=body.name, monthly_fee=to_paise(body.monthly_fee),
                             billing_day=body.billing_day, start_period=body.start_period,
                             guardian_name=body.guardian_name, phone=body.phone,
                             concession_pct=body.concession_pct, upi_ids=body.upi_ids)
        return _with_etag(view, response)

    @app.get("/members")
    def members(active_only: bool = False, inst=Depends(institution), c=Depends(conn)):
        return list_members(c, inst, active_only=active_only)

    @app.get("/members/{member_id}")
    def member(member_id: int, response: Response, inst=Depends(institution), c=Depends(conn)):
        return _with_etag(get_member(c, inst, member_id), response)

    @app.patch("/members/{member_id}")
    def edit_member(member_id: int, body: MemberPatch, response: Response,
                    if_match: str | None = Header(None), inst=Depends(institution), c=Depends(conn)):
        version = _version_from(if_match)
        changes = body.model_dump(exclude_unset=True,
                                  include={"name", "guardian_name", "phone", "billing_day", "concession_pct"})
        fee = (body.fee_change.effective_period, to_paise(body.fee_change.monthly_fee)) if body.fee_change else None
        view = update_member(c, inst, member_id, version, changes=changes, fee_change=fee,
                             add_upi_ids=body.add_upi_ids, remove_upi_ids=body.remove_upi_ids)
        return _with_etag(view, response)

    @app.post("/members/{member_id}/deactivate")
    def deactivate(member_id: int, response: Response, if_match: str | None = Header(None),
                   inst=Depends(institution), c=Depends(conn)):
        return _with_etag(deactivate_member(c, inst, member_id, _version_from(if_match)), response)

    @app.get("/members/{member_id}/statement")
    def member_statement(member_id: int, inst=Depends(institution), c=Depends(conn)):
        return statement(c, inst, member_id, today())

    # bank credits

    @app.post("/bank-credits", status_code=201)
    def add_credit(body: CreditIn, response: Response, inst=Depends(institution), c=Depends(conn)):
        fields = {"utr": body.utr, "channel": body.channel, "payer_upi_id": body.payer_upi_id,
                  "payer_name": body.payer_name, "note": body.note}
        if body.narration:
            parsed = parse_narration(body.narration)
            if parsed is None and not body.utr:
                raise ValueError("narration not recognised; send the utr and payer fields instead")
            fields.update({k: v for k, v in (parsed or {}).items() if v is not None})
        if not fields["utr"]:
            raise ValueError("utr is required")
        received_at = body.received_at                 # a time with no zone is Indian time
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=IST)
        result = record_credit(c, inst, Credit(amount=to_paise(body.amount), received_at=received_at,
                                               narration=body.narration, **fields))
        if result["duplicate"]:
            response.status_code = 200          # nothing was created
        return result

    @app.post("/bank-credits/import")
    def import_csv(file: UploadFile = File(...), workers: int = Query(8, ge=1, le=64),
                   account: str = Query("main", max_length=40), inst=Depends(institution)):
        data = file.file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(status_code=413, detail="statement is larger than 10 MB")
        try:
            text = data.decode("utf-8-sig")          # utf-8-sig strips the byte-order mark Excel adds
        except UnicodeDecodeError:
            raise ValueError("statement must be a UTF-8 CSV file") from None
        # Keep connections free for other requests while the import runs.
        return import_statement(pool, inst, text, workers=min(workers, max(1, pool_size - 2)), account=account)

    @app.get("/bank-credits/{credit_id}")
    def bank_credit(credit_id: int, inst=Depends(institution), c=Depends(conn)):
        return get_credit(c, inst, credit_id)

    @app.post("/bank-credits/{credit_id}/reassign")
    def reassign(credit_id: int, body: ReassignIn, response: Response, who: Actor = Depends(actor),
                 c=Depends(conn)):
        result = request_reassign(c, who.institution_id, who, credit_id, _parts(body.parts))
        if result["status"] == "pending_approval":
            response.status_code = 202          # accepted, not done: a second person must approve
        return result

    @app.post("/bank-credits/{credit_id}/reverse")
    def reverse(credit_id: int, body: ReverseIn, inst=Depends(institution), c=Depends(conn)):
        return reverse_credit(c, inst, credit_id, reason=body.reason, reference=body.reference)

    # review queue

    @app.get("/review")
    def review(limit: int = Query(50, ge=1, le=200), inst=Depends(institution), c=Depends(conn)):
        return review_queue(c, inst, today(), limit)

    @app.post("/review/{credit_id}/assign")
    def assign(credit_id: int, body: AssignIn, inst=Depends(institution), c=Depends(conn)):
        return assign_credit(c, inst, credit_id, body.member_id, body.remember_upi_id)

    @app.post("/review/{credit_id}/split")
    def split(credit_id: int, body: SplitIn, inst=Depends(institution), c=Depends(conn)):
        return split_credit(c, inst, credit_id, _parts(body.parts), body.remember_upi_id)

    # jobs

    @app.post("/billing/run")
    def billing(as_of: date | None = None, inst=Depends(institution), c=Depends(conn)):
        day = as_of or today()
        billed = run_billing(c, inst, day)
        late = apply_late_fees(c, inst, day)
        return {**billed, "late_fees": len(late)}

    @app.post("/reminders/run")
    def reminders(inst=Depends(institution), c=Depends(conn)):
        return {"queued": queue_reminders(c, inst, today())}

    @app.post("/outbox/relay")
    def relay(inst=Depends(institution), c=Depends(conn)):
        return relay_once(c, inst, send, now=now)

    @app.get("/outbox/dead")
    def dead(inst=Depends(institution), c=Depends(conn)):
        return dead_messages(c, inst)

    # staff and approvals

    @app.get("/me")
    def me(who: Actor = Depends(actor)):
        return {"staff_id": who.staff_id, "name": who.name, "role": who.role,
                "institution_id": who.institution_id}

    @app.post("/staff", status_code=201)
    def new_staff(body: StaffIn, who: Actor = Depends(owner), c=Depends(conn)):
        return add_staff(c, who.institution_id, body.name, body.role)

    @app.get("/staff")
    def staff(who: Actor = Depends(owner), c=Depends(conn)):
        return list_staff(c, who.institution_id)

    @app.post("/staff/{staff_id}/deactivate")
    def remove_staff(staff_id: int, who: Actor = Depends(owner), c=Depends(conn)):
        return deactivate_staff(c, who.institution_id, staff_id)

    @app.get("/approvals")
    def approvals(status: str | None = "pending", inst=Depends(institution), c=Depends(conn)):
        return list_approvals(c, inst, None if status == "all" else status)

    @app.post("/approvals/{approval_id}/approve")
    def approve(approval_id: int, body: DecisionIn | None = None, who: Actor = Depends(actor),
                c=Depends(conn)):
        return decide(c, who.institution_id, who, approval_id, approve=True, note=body.note if body else None)

    @app.post("/approvals/{approval_id}/reject")
    def reject(approval_id: int, body: DecisionIn | None = None, who: Actor = Depends(actor),
               c=Depends(conn)):
        return decide(c, who.institution_id, who, approval_id, approve=False, note=body.note if body else None)

    # daily close and alerts

    @app.post("/close/run")
    def close(day: date | None = None, inst=Depends(institution), c=Depends(conn)):
        return run_close(c, inst, day or today())

    @app.get("/close")
    def closes(inst=Depends(institution), c=Depends(conn)):
        return recent_closes(c, inst)

    @app.get("/statements")
    def statements(inst=Depends(institution), c=Depends(conn)):
        return list_statements(c, inst)

    @app.get("/alerts")
    def alerts(status: str | None = "open", inst=Depends(institution), c=Depends(conn)):
        return list_alerts(c, inst, status=None if status == "all" else status)

    @app.post("/alerts/{alert_id}/resolve")
    def resolve(alert_id: int, inst=Depends(institution), c=Depends(conn)):
        return resolve_alert(c, inst, alert_id)

    # reports

    @app.get("/reports/collection")
    def collection(period: str | None = None, inst=Depends(institution), c=Depends(conn)):
        return collection_report(c, inst, period or period_of(today()))

    @app.get("/reports/ageing")
    def ageing_report(as_of: date | None = None, inst=Depends(institution), c=Depends(conn)):
        return ageing(c, inst, as_of or today())

    @app.get("/reports/defaulters")
    def defaulters_report(min_days: int = Query(30, ge=0), as_of: date | None = None,
                          inst=Depends(institution), c=Depends(conn)):
        return defaulters(c, inst, as_of or today(), min_days)

    @app.get("/reports/matching")
    def matching(inst=Depends(institution), c=Depends(conn)):
        return matching_report(c, inst, now())

    @app.get("/audit")
    def audit_log(limit: int = Query(100, ge=1, le=1000), inst=Depends(institution), c=Depends(conn)):
        return audit.recent(c, inst, limit)

    return app
