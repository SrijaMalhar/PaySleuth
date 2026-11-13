"""Monthly billing with catch-up, and late fees.

Every function that bills takes the member lock first, so billing, payments
and late fees for one member happen one at a time.
"""

from datetime import date, timedelta

from . import audit
from .allocation import settle_member
from .db import lock_members, tenant_tx
from .institutions import settings
from .periods import due_date, next_period, period_of, periods_from


def concession_amount(gross: int, pct: int) -> int:
    """Concession in paise, rounded to the nearest paisa with halves up. No floats."""
    return (gross * pct + 50) // 100


def _fee_at(fees: list[dict], period: str) -> int | None:
    fee = None
    for row in fees:                      # ordered by effective_period
        if row["effective_period"] <= period:
            fee = row["monthly_fee"]
    return fee


def bill_member(conn, inst: int, member_id: int, target: str) -> list[dict]:
    """Bill every unbilled month up to target, then apply any advance to the new charges.

    billed_through, not "which charges exist", decides where catch-up starts:
    a 100% scholarship month creates no charge, and without billed_through,
    ending the scholarship would make catch-up bill those months later.
    """
    with tenant_tx(conn, inst):
        lock_members(conn, [member_id])
        member = conn.execute("SELECT * FROM members WHERE id = %s", (member_id,)).fetchone()
        if not member["active"]:
            return []
        first = next_period(member["billed_through"]) if member["billed_through"] else member["start_period"]
        months = periods_from(first, target)
        if not months:
            return []
        fees = conn.execute("SELECT effective_period, monthly_fee FROM member_fees "
                            "WHERE member_id = %s ORDER BY effective_period", (member_id,)).fetchall()
        created = []
        for period in months:
            gross = _fee_at(fees, period)
            concession = concession_amount(gross, member["concession_pct"])
            amount = gross - concession
            if amount == 0:                           # a full scholarship month
                continue
            row = conn.execute(
                """INSERT INTO charges (institution_id, member_id, period, kind, gross, concession,
                                        amount, due_date)
                   VALUES (%s, %s, %s, 'monthly_fee', %s, %s, %s, %s)
                   ON CONFLICT (member_id, period, kind) DO NOTHING
                   RETURNING id, period, amount, due_date""",
                (inst, member_id, period, gross, concession, amount,
                 due_date(period, member["billing_day"]))).fetchone()
            if row:
                created.append(row)
        conn.execute("UPDATE members SET billed_through = %s WHERE id = %s", (target, member_id))
        settle_member(conn, inst, member_id)          # an advance pays the new charges at once
        if created:
            audit.record(conn, inst, "member.billed", "member", member_id,
                         periods=[c["period"] for c in created])
        return created


def run_billing(conn, inst: int, as_of: date) -> dict:
    """Bill every active member through the month of as_of.

    One transaction per member, so one bad member does not block the rest and
    no lock is held for long.
    """
    target = period_of(as_of)
    with tenant_tx(conn, inst):
        ids = [r["id"] for r in conn.execute(
            """SELECT id FROM members WHERE active AND start_period <= %s
                 AND (billed_through IS NULL OR billed_through < %s) ORDER BY id""",
            (target, target)).fetchall()]
    charges = 0
    for member_id in ids:
        charges += len(bill_member(conn, inst, member_id, target))
    return {"period": target, "members": len(ids), "charges_created": charges}


def _unpaid_monthly_fee(conn, member_id: int, period: str) -> bool:
    row = conn.execute(
        """SELECT c.amount - COALESCE(SUM(a.amount), 0) AS unpaid
             FROM charges c LEFT JOIN allocations a ON a.charge_id = c.id
            WHERE c.member_id = %s AND c.period = %s AND c.kind = 'monthly_fee'
            GROUP BY c.id""", (member_id, period)).fetchone()
    return bool(row and row["unpaid"] > 0)


def apply_late_fees(conn, inst: int, today: date) -> list[dict]:
    """Add one late fee per unpaid month once its grace period has passed.

    Check, lock, re-check: candidates are found without locks, then each one
    is re-checked under the member lock, because a payment may land between
    the scan and the insert.
    """
    cfg = settings(conn, inst)
    if cfg["late_fee"] == 0:
        return []
    cutoff = today - timedelta(days=cfg["grace_days"])
    with tenant_tx(conn, inst):
        candidates = conn.execute(
            """SELECT c.member_id, c.period
                 FROM charges c JOIN members m ON m.id = c.member_id
                 LEFT JOIN allocations a ON a.charge_id = c.id
                WHERE c.kind = 'monthly_fee' AND c.due_date < %s AND m.active
                  AND NOT EXISTS (SELECT 1 FROM charges l WHERE l.member_id = c.member_id
                                    AND l.period = c.period AND l.kind = 'late_fee')
                GROUP BY c.id HAVING c.amount - COALESCE(SUM(a.amount), 0) > 0
                ORDER BY c.member_id, c.period""", (cutoff,)).fetchall()

    created = []
    for candidate in candidates:
        with tenant_tx(conn, inst):
            lock_members(conn, [candidate["member_id"]])
            if not _unpaid_monthly_fee(conn, candidate["member_id"], candidate["period"]):
                continue                              # paid while we waited for the lock
            row = conn.execute(
                """INSERT INTO charges (institution_id, member_id, period, kind, gross, amount, due_date)
                   VALUES (%s, %s, %s, 'late_fee', %s, %s, %s)
                   ON CONFLICT (member_id, period, kind) DO NOTHING
                   RETURNING id, member_id, period, amount""",
                (inst, candidate["member_id"], candidate["period"], cfg["late_fee"], cfg["late_fee"],
                 today)).fetchone()
            if row:
                settle_member(conn, inst, candidate["member_id"])
                audit.record(conn, inst, "charge.late_fee", "member", candidate["member_id"],
                             period=candidate["period"], amount=cfg["late_fee"])
                created.append(row)
    return created
