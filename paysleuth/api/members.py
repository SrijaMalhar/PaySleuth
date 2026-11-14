"""Members: the people who owe money, and the fee history behind each one.

Edits use optimistic locking -- the version is surfaced as an ETag and
required back as If-Match -- because a human filling a form holds it open for
minutes and a pessimistic lock across that is wrong.
"""

from fastapi import APIRouter, Depends, Header, Response

from ..members import create_member, deactivate_member, get_member, list_members, update_member
from ..money import to_paise
from ..reports import statement
from .deps import conn, institution, page, today
from .schemas import MemberIn, MemberOut, MemberPatch, MemberSummary, version_from, with_etag

router = APIRouter(tags=["members"])


@router.post("/members", status_code=201, response_model=MemberOut)
def add_member(body: MemberIn, response: Response, inst=Depends(institution), c=Depends(conn)):
    view = create_member(c, inst, name=body.name, monthly_fee=to_paise(body.monthly_fee),
                         billing_day=body.billing_day, start_period=body.start_period,
                         guardian_name=body.guardian_name, phone=body.phone,
                         concession_pct=body.concession_pct, upi_ids=body.upi_ids)
    return with_etag(view, response)


@router.get("/members", response_model=list[MemberSummary])
def members(active_only: bool = False, paging=Depends(page),
            inst=Depends(institution), c=Depends(conn)):
    limit, offset = paging
    return list_members(c, inst, active_only=active_only, limit=limit, offset=offset)


@router.get("/members/{member_id}", response_model=MemberOut)
def member(member_id: int, response: Response, inst=Depends(institution), c=Depends(conn)):
    return with_etag(get_member(c, inst, member_id), response)


@router.patch("/members/{member_id}", response_model=MemberOut)
def edit_member(member_id: int, body: MemberPatch, response: Response,
                if_match: str | None = Header(None), inst=Depends(institution), c=Depends(conn)):
    version = version_from(if_match)
    changes = body.model_dump(exclude_unset=True,
                              include={"name", "guardian_name", "phone", "billing_day",
                                       "concession_pct"})
    fee = ((body.fee_change.effective_period, to_paise(body.fee_change.monthly_fee))
           if body.fee_change else None)
    view = update_member(c, inst, member_id, version, changes=changes, fee_change=fee,
                         add_upi_ids=body.add_upi_ids, remove_upi_ids=body.remove_upi_ids)
    return with_etag(view, response)


@router.post("/members/{member_id}/deactivate", response_model=MemberOut)
def deactivate(member_id: int, response: Response, if_match: str | None = Header(None),
               inst=Depends(institution), c=Depends(conn)):
    return with_etag(deactivate_member(c, inst, member_id, version_from(if_match)), response)


@router.get("/members/{member_id}/statement")
def member_statement(member_id: int, inst=Depends(institution), c=Depends(conn),
                     day=Depends(today)):
    return statement(c, inst, member_id, day)
