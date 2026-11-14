"""Staff keys and the maker-checker queue.

A correction over the institution's threshold needs a second person, and the
database refuses a self-approval whatever this layer does.
"""

from fastapi import APIRouter, Depends

from ..actors import Actor
from ..approvals import decide, list_approvals
from ..institutions import add_staff, deactivate_staff, list_staff
from .deps import actor, conn, institution, owner, page
from .schemas import ApprovalOut, DecisionIn, MeOut, NewStaffOut, StaffIn, StaffOut

router = APIRouter(tags=["staff"])


@router.get("/me", response_model=MeOut)
def me(who: Actor = Depends(actor)):
    return {"staff_id": who.staff_id, "name": who.name, "role": who.role,
            "institution_id": who.institution_id}


@router.post("/staff", status_code=201, response_model=NewStaffOut)
def new_staff(body: StaffIn, who: Actor = Depends(owner), c=Depends(conn)):
    return add_staff(c, who.institution_id, body.name, body.role)


@router.get("/staff", response_model=list[StaffOut])
def staff(paging=Depends(page), who: Actor = Depends(owner), c=Depends(conn)):
    limit, offset = paging
    return list_staff(c, who.institution_id, limit=limit, offset=offset)


@router.post("/staff/{staff_id}/deactivate", response_model=StaffOut)
def remove_staff(staff_id: int, who: Actor = Depends(owner), c=Depends(conn)):
    return deactivate_staff(c, who.institution_id, staff_id)


@router.get("/approvals", response_model=list[ApprovalOut])
def approvals(status: str | None = "pending", paging=Depends(page),
              inst=Depends(institution), c=Depends(conn)):
    limit, offset = paging
    return list_approvals(c, inst, None if status == "all" else status,
                          limit=limit, offset=offset)


@router.post("/approvals/{approval_id}/approve")
def approve(approval_id: int, body: DecisionIn | None = None, who: Actor = Depends(actor),
            c=Depends(conn)):
    return decide(c, who.institution_id, who, approval_id, approve=True,
                  note=body.note if body else None)


@router.post("/approvals/{approval_id}/reject")
def reject(approval_id: int, body: DecisionIn | None = None, who: Actor = Depends(actor),
           c=Depends(conn)):
    return decide(c, who.institution_id, who, approval_id, approve=False,
                  note=body.note if body else None)
