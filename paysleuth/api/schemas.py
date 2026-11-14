"""Request bodies, and the ETag helpers that go with them.

Money crosses this boundary as a string such as "2000.50", never a JSON
number, because many clients parse JSON numbers as floats and a float is not
an amount.
"""

import re
from datetime import datetime

from fastapi import HTTPException, Response
from pydantic import BaseModel, Field

from ..errors import PreconditionRequired
from ..money import to_paise


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


def parts_to_paise(parts: list[Part]) -> list[tuple[int, int]]:
    return [(p.member_id, to_paise(p.amount)) for p in parts]


# --- ETags -----------------------------------------------------------------------------

def etag(version: int) -> str:
    return f'"{version}"'


def version_from(if_match: str | None) -> int:
    if not if_match:
        raise PreconditionRequired("send If-Match with the ETag from GET /members/{id}")
    m = re.fullmatch(r'\s*(?:W/)?"(\d+)"\s*', if_match)
    if not m:
        raise HTTPException(status_code=400, detail='If-Match must be an ETag like "3"')
    return int(m.group(1))


def with_etag(view: dict, response: Response) -> dict:
    response.headers["ETag"] = etag(view["version"])
    return view


# --- response bodies -------------------------------------------------------------------
#
# These are what /docs shows a client it will get back, and FastAPI filters the
# response through them -- so a field missing here disappears from the API.
# Each one was written against a real response rather than from reading the
# function, and the shapes are asserted in tests/test_schemas.py.
#
# Not every endpoint has one. The daily close and the statement list carry a
# jsonb `figures` whose keys are the close's own vocabulary, and the matching
# report's `signals` block is keyed by signal name, so both are open by design;
# typing them would mean freezing a shape that is meant to grow. Those stay
# dict-typed, which is a statement about the data rather than an omission.

class MemberSummary(BaseModel):
    """One row of GET /members. The money is a string, like everywhere else."""
    id: int
    ref_code: str
    name: str
    guardian_name: str | None
    active: bool
    outstanding: str = Field(examples=["2100.00"])
    credit: str = Field(examples=["0.00"])


class FeeAt(BaseModel):
    effective_period: str = Field(examples=["2026-06"])
    monthly_fee: str


class UpiIdOut(BaseModel):
    upi_id: str
    source: str = Field(description="registered by hand, or learned from a resolved credit")


class MemberOut(MemberSummary):
    """GET /members/{id}, and what every member write returns.

    Carries `version`, which is the ETag: an edit must send it back as
    If-Match or get a 428, and a stale one gets a 412.
    """
    phone: str | None
    billing_day: int
    start_period: str
    billed_through: str | None
    concession_pct: int
    version: int
    monthly_fee: str
    fee_history: list[FeeAt]
    upi_ids: list[UpiIdOut]


class CreditOut(BaseModel):
    id: int
    utr: str
    channel: str
    amount: str
    payer_upi_id: str | None
    payer_name: str | None
    note: str | None
    received_at: str
    status: str = Field(examples=["matched"], description="matched | needs_review | reversed")
    match_rule: str | None = Field(description="which of the five rules decided, if any")


class AppliedPart(BaseModel):
    member_id: int
    amount: str


class RecordCreditOut(BaseModel):
    credit: CreditOut
    duplicate: bool = Field(description="true when this UTR was already recorded")
    applied: list[AppliedPart]


class Suggestion(BaseModel):
    """A ranked guess, shown to a person and never applied on its own."""
    member_id: int
    ref_code: str
    name: str
    score: int
    signals: list[str]
    reasons: list[str] = Field(description="one sentence per signal, for the owner to read")


class ReviewItem(CreditOut):
    suggestions: list[Suggestion]


class CreditPart(BaseModel):
    member_id: int
    ref_code: str
    amount: str


class PaymentRow(CreditPart):
    payment_id: int
    corrects_payment_id: int | None
    recorded_at: str


class CreditDetail(CreditOut):
    """GET /bank-credits/{id}: where the money is now, and every row ever written.

    `parts` is what stands; `history` is the append-only trail including the
    corrections, which is the thing to show a parent who says they paid in May.
    """
    parts: list[CreditPart]
    history: list[PaymentRow]


class StaffOut(BaseModel):
    id: int
    name: str
    role: str
    active: bool


class NewStaffOut(StaffOut):
    api_key: str = Field(description="shown once; only its hash is stored")


class MeOut(BaseModel):
    staff_id: int
    name: str
    role: str
    institution_id: int


class HealthOut(BaseModel):
    status: str


class AlertOut(BaseModel):
    id: int
    kind: str
    message: str
    details: dict
    status: str
    created_at: str
    resolved_at: str | None


class ApprovalOut(BaseModel):
    id: int
    action: str
    bank_credit_id: int
    utr: str
    amount: str
    payload: dict
    status: str
    note: str | None
    requested_by: int
    requested_by_name: str
    decided_by: int | None
    decided_by_name: str | None
    created_at: str
    decided_at: str | None


class DefaulterOut(BaseModel):
    member_id: int
    ref_code: str
    name: str
    phone: str | None
    overdue: str
    oldest_days_overdue: int


class DeadMessageOut(BaseModel):
    id: int
    message_id: str
    kind: str
    member_id: int | None
    attempts: int
    last_error: str | None
    created_at: str


class AuditOut(BaseModel):
    id: int
    action: str
    entity: str
    entity_id: int | None
    details: dict
    created_at: str
    actor_id: int | None
    actor: str
