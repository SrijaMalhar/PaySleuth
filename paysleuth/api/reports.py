"""Reports, and the audit trail.

Every figure here is derived from charges, payments and allocations at read
time. There is no reporting table to fall out of step with the books.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query

from .. import audit
from ..evaluation import matching_report
from ..periods import period_of
from ..reports import ageing, collection_report, defaulters
from .deps import conn, institution, now, page, today
from .schemas import AuditOut, DefaulterOut

router = APIRouter(tags=["reports"])


@router.get("/reports/collection")
def collection(period: str | None = None, inst=Depends(institution), c=Depends(conn),
               day=Depends(today)):
    return collection_report(c, inst, period or period_of(day))


@router.get("/reports/ageing")
def ageing_report(as_of: date | None = None, inst=Depends(institution), c=Depends(conn),
                  day=Depends(today)):
    return ageing(c, inst, as_of or day)


@router.get("/reports/defaulters", response_model=list[DefaulterOut])
def defaulters_report(min_days: int = Query(30, ge=0), as_of: date | None = None,
                      paging=Depends(page), inst=Depends(institution), c=Depends(conn),
                      day=Depends(today)):
    limit, offset = paging
    return defaulters(c, inst, as_of or day, min_days, limit=limit, offset=offset)


@router.get("/reports/matching")
def matching(inst=Depends(institution), c=Depends(conn), at=Depends(now)):
    return matching_report(c, inst, at)


@router.get("/audit", response_model=list[AuditOut])
def audit_log(paging=Depends(page), inst=Depends(institution), c=Depends(conn)):
    limit, offset = paging
    return audit.recent(c, inst, limit=limit, offset=offset)
