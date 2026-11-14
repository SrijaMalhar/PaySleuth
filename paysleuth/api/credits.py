"""Bank credits and the queue of the ones no rule was confident about.

The review queue is the only place a person spends money by hand, so every
suggestion it carries is ranked and explained, and none of them is applied.
"""

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile

from ..approvals import request_reassign
from ..actors import Actor
from ..banks.common import full_upi_id
from ..importer import import_statement
from ..money import to_paise
from ..payments import (assign_credit, get_credit, record_credit, reverse_credit, review_queue,
                        split_credit)
from ..periods import IST
from ..statements import Credit, parse_narration
from .deps import actor, conn, institution, page, today
from .schemas import AssignIn, CreditIn, ReassignIn, ReverseIn, SplitIn, parts_to_paise

MAX_UPLOAD = 10 * 1024 * 1024

router = APIRouter(tags=["bank credits"])


@router.post("/bank-credits", status_code=201)
def add_credit(body: CreditIn, response: Response, inst=Depends(institution), c=Depends(conn)):
    # A UPI ID that is cut short or masked is dropped rather than refused: the
    # credit is real money and must be recorded, while the ID is only a hint
    # for matching. Anything else malformed is still the caller's error.
    fields = {"utr": body.utr, "channel": body.channel,
              "payer_upi_id": full_upi_id(body.payer_upi_id),
              "payer_name": body.payer_name, "note": body.note}
    if body.narration:
        parsed = parse_narration(body.narration)
        if parsed is None and not body.utr:
            raise ValueError("narration not recognised; send the utr and payer fields instead")
        # The narration's fields win where it has them, and the caller's
        # remaining fields stand: a caller who sends both a narration and an
        # explicit note means the note. A masked UPI ID in either is already
        # None by this point, so neither can reinstate one.
        fields.update({k: v for k, v in (parsed or {}).items() if v is not None})
    if not fields["utr"]:
        raise ValueError("utr is required")
    received_at = body.received_at                 # a time with no zone is Indian time
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=IST)
    if not 2 <= received_at.year <= 9998:          # leave room for the zone shift either way
        raise ValueError("received_at must be a plausible date")
    result = record_credit(c, inst, Credit(amount=to_paise(body.amount), received_at=received_at,
                                           narration=body.narration, **fields))
    if result["duplicate"]:
        response.status_code = 200          # nothing was created
    return result


@router.post("/bank-credits/import")
def import_csv(request: Request, file: UploadFile = File(...),
               workers: int = Query(8, ge=1, le=64),
               account: str = Query("main", max_length=40, pattern=r"^[A-Za-z0-9 _-]+$"),
               inst=Depends(institution)):
    data = file.file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="statement is larger than 10 MB")
    try:
        text = data.decode("utf-8-sig")          # utf-8-sig strips the byte-order mark Excel adds
    except UnicodeDecodeError:
        raise ValueError("statement must be a UTF-8 CSV file") from None
    if "\x00" in text:
        raise ValueError("statement must be a UTF-8 CSV file, not a binary file")
    # Keep connections free for other requests while the import runs.
    pool, size = request.app.state.pool, request.app.state.pool_size
    return import_statement(pool, inst, text, workers=min(workers, max(1, size - 2)),
                            account=account)


@router.get("/bank-credits/{credit_id}")
def bank_credit(credit_id: int, inst=Depends(institution), c=Depends(conn)):
    return get_credit(c, inst, credit_id)


@router.post("/bank-credits/{credit_id}/reassign")
def reassign(credit_id: int, body: ReassignIn, response: Response, who: Actor = Depends(actor),
             c=Depends(conn)):
    result = request_reassign(c, who.institution_id, who, credit_id, parts_to_paise(body.parts))
    if result["status"] == "pending_approval":
        response.status_code = 202          # accepted, not done: a second person must approve
    return result


@router.post("/bank-credits/{credit_id}/reverse")
def reverse(credit_id: int, body: ReverseIn, inst=Depends(institution), c=Depends(conn)):
    return reverse_credit(c, inst, credit_id, reason=body.reason, reference=body.reference)


@router.get("/review")
def review(paging=Depends(page), inst=Depends(institution), c=Depends(conn), day=Depends(today)):
    limit, _ = paging
    return review_queue(c, inst, day, limit)


@router.post("/review/{credit_id}/assign")
def assign(credit_id: int, body: AssignIn, inst=Depends(institution), c=Depends(conn)):
    return assign_credit(c, inst, credit_id, body.member_id, body.remember_upi_id)


@router.post("/review/{credit_id}/split")
def split(credit_id: int, body: SplitIn, inst=Depends(institution), c=Depends(conn)):
    return split_credit(c, inst, credit_id, parts_to_paise(body.parts), body.remember_upi_id)
