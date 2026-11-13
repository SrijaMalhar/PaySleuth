"""Members, their fee history and their registered UPI IDs."""

import re
import uuid

from . import audit
from .allocation import balances
from .db import lock_members, tenant_tx
from .errors import Conflict, NotFound, PreconditionFailed
from .money import check_text, to_rupees
from .periods import check_period

EDITABLE = {"name", "guardian_name", "phone", "billing_day", "concession_pct"}

_UPI_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,255}@[a-z][a-z0-9.]{1,63}$")
_PHONE = re.compile(r"^[6-9]\d{9}$")


def normalise_upi_id(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    upi_id = str(value).strip().lower()
    if not _UPI_ID.match(upi_id):
        raise ValueError(f"not a UPI ID: {value!r}")
    return upi_id


def normalise_phone(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    digits = re.sub(r"[\s\-()]", "", str(value))
    if digits.startswith("+91"):
        digits = digits[3:]
    elif len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not _PHONE.match(digits):
        raise ValueError(f"phone must be a 10-digit Indian mobile number (got {value!r})")
    return digits


def _clean_name(value: str | None, field: str, *, required: bool) -> str | None:
    text = " ".join(check_text(value, field).split())
    if not text:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if len(text) > 120:
        raise ValueError(f"{field} must be at most 120 characters")
    return text


def _check_fee(monthly_fee: int) -> int:
    if not isinstance(monthly_fee, int) or isinstance(monthly_fee, bool) or monthly_fee <= 0:
        raise ValueError("monthly fee must be a positive number of paise")
    return monthly_fee


def _check_billing_day(day: int) -> int:
    if not isinstance(day, int) or not 1 <= day <= 31:
        raise ValueError("billing day must be between 1 and 31")
    return day


def _check_concession(pct: int) -> int:
    if not isinstance(pct, int) or not 0 <= pct <= 100:
        raise ValueError("concession must be between 0 and 100 percent")
    return pct


def _validate_changes(changes: dict) -> dict:
    unknown = set(changes) - EDITABLE
    if unknown:
        raise ValueError(f"cannot edit {sorted(unknown)}")
    values = {}
    for column, value in changes.items():
        if column == "name":
            values[column] = _clean_name(value, "name", required=True)
        elif column == "guardian_name":
            values[column] = _clean_name(value, "guardian name", required=False)
        elif column == "phone":
            values[column] = normalise_phone(value)
        elif column == "billing_day":
            values[column] = _check_billing_day(value)
        elif column == "concession_pct":
            values[column] = _check_concession(value)
    return values


def create_member(conn, inst: int, *, name: str, monthly_fee: int, billing_day: int,
                  start_period: str, guardian_name: str | None = None, phone: str | None = None,
                  concession_pct: int = 0, upi_ids=()) -> dict:
    # Validate everything before opening the transaction.
    name = _clean_name(name, "name", required=True)
    guardian_name = _clean_name(guardian_name, "guardian name", required=False)
    phone = normalise_phone(phone)
    _check_fee(monthly_fee)
    _check_billing_day(billing_day)
    check_period(start_period)
    _check_concession(concession_pct)
    upi = sorted({u for u in (normalise_upi_id(x) for x in upi_ids) if u})

    with tenant_tx(conn, inst):
        # ref_code comes from the id, which does not exist until the insert, and
        # the column is NOT NULL UNIQUE. A random placeholder is unique, and both
        # statements run in one transaction, so nobody ever sees it.
        member_id = conn.execute(
            """INSERT INTO members (institution_id, ref_code, name, guardian_name, phone,
                                    billing_day, start_period, concession_pct)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (inst, f"tmp-{uuid.uuid4()}", name, guardian_name, phone, billing_day,
             start_period, concession_pct)).fetchone()["id"]
        ref_code = f"PS{1000 + member_id}"
        conn.execute("UPDATE members SET ref_code = %s WHERE id = %s", (ref_code, member_id))
        conn.execute("INSERT INTO member_fees (institution_id, member_id, effective_period, monthly_fee) "
                     "VALUES (%s, %s, %s, %s)", (inst, member_id, start_period, monthly_fee))
        for upi_id in upi:
            conn.execute("INSERT INTO member_upi_ids (institution_id, upi_id, member_id, source) "
                         "VALUES (%s, %s, %s, 'registered')", (inst, upi_id, member_id))
        audit.record(conn, inst, "member.created", "member", member_id, ref_code=ref_code,
                     monthly_fee=monthly_fee, start_period=start_period)
        return _view(conn, _row(conn, member_id))


def _row(conn, member_id: int) -> dict:
    row = conn.execute("SELECT * FROM members WHERE id = %s", (member_id,)).fetchone()
    if row is None:
        raise NotFound(f"member {member_id} not found")
    return row


def _view(conn, row: dict) -> dict:
    fees = conn.execute("SELECT effective_period, monthly_fee FROM member_fees "
                        "WHERE member_id = %s ORDER BY effective_period", (row["id"],)).fetchall()
    upi = conn.execute("SELECT upi_id, source FROM member_upi_ids WHERE member_id = %s ORDER BY upi_id",
                       (row["id"],)).fetchall()
    money = balances(conn, [row["id"]])[row["id"]]
    return {
        "id": row["id"], "ref_code": row["ref_code"], "name": row["name"],
        "guardian_name": row["guardian_name"], "phone": row["phone"],
        "billing_day": row["billing_day"], "start_period": row["start_period"],
        "billed_through": row["billed_through"], "concession_pct": row["concession_pct"],
        "active": row["active"], "version": row["version"],
        "monthly_fee": to_rupees(fees[-1]["monthly_fee"]),
        "fee_history": [{"effective_period": f["effective_period"],
                         "monthly_fee": to_rupees(f["monthly_fee"])} for f in fees],
        "upi_ids": upi,
        "outstanding": to_rupees(money["outstanding"]),
        "credit": to_rupees(money["credit"]),
    }


def get_member(conn, inst: int, member_id: int) -> dict:
    with tenant_tx(conn, inst):
        return _view(conn, _row(conn, member_id))


def fee_for_period(conn, member_id: int, period: str) -> int | None:
    """The fee in force for a month: the latest change at or before it."""
    row = conn.execute(
        "SELECT monthly_fee FROM member_fees WHERE member_id = %s AND effective_period <= %s "
        "ORDER BY effective_period DESC LIMIT 1", (member_id, period)).fetchone()
    return row["monthly_fee"] if row else None


def _check_version(conn, member_id: int, expected_version: int, sql: str, params: dict) -> int:
    # The check and the write are one statement, so there is no gap between
    # "is it still version 3?" and "save".
    updated = conn.execute(sql, {**params, "id": member_id, "expected": expected_version}).fetchone()
    if updated is None:
        current = conn.execute("SELECT version FROM members WHERE id = %s", (member_id,)).fetchone()
        if current is None:
            raise NotFound(f"member {member_id} not found")
        raise PreconditionFailed(f"member {member_id} was changed by someone else; reload and try again")
    return updated["version"]


def update_member(conn, inst: int, member_id: int, expected_version: int, *, changes=None,
                  fee_change=None, add_upi_ids=(), remove_upi_ids=()) -> dict:
    """Edit a member, failing with PreconditionFailed if it changed since it was read.

    fee_change is (effective_period, monthly_fee_paise).
    """
    values = _validate_changes(dict(changes or {}))
    if fee_change is not None:
        check_period(fee_change[0])
        _check_fee(fee_change[1])
    add = sorted({u for u in (normalise_upi_id(x) for x in add_upi_ids) if u})
    remove = sorted({u for u in (normalise_upi_id(x) for x in remove_upi_ids) if u})

    with tenant_tx(conn, inst):
        # Column names come only from EDITABLE, so no user text reaches the SQL;
        # the values still go through placeholders.
        sets = ", ".join(f"{column} = %({column})s" for column in sorted(values))
        sql = (f"UPDATE members SET {sets + ', ' if sets else ''}version = version + 1, updated_at = now() "
               f"WHERE id = %(id)s AND version = %(expected)s RETURNING version")
        _check_version(conn, member_id, expected_version, sql, values)
        if fee_change is not None:
            _change_fee(conn, inst, member_id, *fee_change)
        for upi_id in add:
            conn.execute("INSERT INTO member_upi_ids (institution_id, upi_id, member_id, source) "
                         "VALUES (%s, %s, %s, 'registered') ON CONFLICT DO NOTHING",
                         (inst, upi_id, member_id))
        if remove:
            conn.execute("DELETE FROM member_upi_ids WHERE member_id = %s AND upi_id = ANY(%s)",
                         (member_id, remove))
        audit.record(conn, inst, "member.updated", "member", member_id, changes=values,
                     fee_change=list(fee_change) if fee_change else None,
                     added_upi_ids=add, removed_upi_ids=remove)
        return _view(conn, _row(conn, member_id))


def _change_fee(conn, inst: int, member_id: int, effective_period: str, monthly_fee: int) -> None:
    """Billed months keep the fee they were billed at; only later months change."""
    member = conn.execute("SELECT start_period, billed_through FROM members WHERE id = %s",
                          (member_id,)).fetchone()
    if member["billed_through"] and effective_period <= member["billed_through"]:
        raise Conflict(f"{effective_period} is already billed (through {member['billed_through']}); "
                       f"a fee change can start from the next unbilled month")
    if effective_period < member["start_period"]:
        raise ValueError("a fee change cannot start before the member's first month")
    conn.execute(
        """INSERT INTO member_fees (institution_id, member_id, effective_period, monthly_fee)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (member_id, effective_period) DO UPDATE SET monthly_fee = EXCLUDED.monthly_fee""",
        (inst, member_id, effective_period, monthly_fee))


def deactivate_member(conn, inst: int, member_id: int, expected_version: int) -> dict:
    """Stop future billing. History, dues and credit stay as they are."""
    with tenant_tx(conn, inst):
        lock_members(conn, [member_id])
        _check_version(conn, member_id, expected_version,
                       "UPDATE members SET active = false, version = version + 1, updated_at = now() "
                       "WHERE id = %(id)s AND version = %(expected)s RETURNING version", {})
        audit.record(conn, inst, "member.deactivated", "member", member_id)
        return _view(conn, _row(conn, member_id))


def list_members(conn, inst: int, *, active_only: bool = False) -> list[dict]:
    with tenant_tx(conn, inst):
        rows = conn.execute("SELECT id, ref_code, name, guardian_name, active FROM members "
                            "WHERE institution_id = %s AND (active OR NOT %s) ORDER BY id",
                            (inst, active_only)).fetchall()
        money = balances(conn, [r["id"] for r in rows])
        return [{**r, "outstanding": to_rupees(money[r["id"]]["outstanding"]),
                 "credit": to_rupees(money[r["id"]]["credit"])} for r in rows]
