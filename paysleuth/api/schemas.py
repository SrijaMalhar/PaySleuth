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
