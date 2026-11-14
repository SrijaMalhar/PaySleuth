"""What an owner reads: member statements, monthly collection, ageing and defaulters.

Every report reads inside tenant_tx and computes from charges and allocations,
never from a stored balance.
"""

from datetime import date

from .allocation import LIVE, balances
from .db import tenant_tx
from .errors import NotFound
from .money import to_rupees
from .periods import IST, check_period
from .paging import limits


def _percent(part: int, whole: int) -> str:
    """part/whole as a percentage with one decimal, rounded half up, in integer arithmetic."""
    if whole == 0:
        return "0.0"
    tenths = (part * 1000 + whole // 2) // whole
    return f"{tenths // 10}.{tenths % 10}"


def statement(conn, inst: int, member_id: int, today: date) -> dict:
    with tenant_tx(conn, inst):
        member = conn.execute("SELECT id, ref_code, name, guardian_name, active FROM members WHERE id = %s",
                              (member_id,)).fetchone()
        if member is None:
            raise NotFound(f"member {member_id} not found")
        charges = conn.execute(
            """SELECT c.id, c.period, c.kind, c.gross, c.concession, c.amount, c.due_date,
                      COALESCE(SUM(a.amount), 0) AS paid
                 FROM charges c LEFT JOIN allocations a ON a.charge_id = c.id
                WHERE c.member_id = %s GROUP BY c.id ORDER BY c.due_date, c.id""", (member_id,)).fetchall()
        payments = conn.execute(
            f"""SELECT b.utr, b.channel, b.match_rule, p.amount, p.received_at
                  FROM payments p JOIN bank_credits b ON b.id = p.bank_credit_id
                 WHERE p.member_id = %s AND {LIVE} ORDER BY p.received_at, p.id""",
            (member_id,)).fetchall()
        totals = balances(conn, [member_id])[member_id]

    lines = []
    for c in charges:
        unpaid = c["amount"] - c["paid"]
        status = "paid" if unpaid == 0 else "partially_paid" if c["paid"] > 0 else "unpaid"
        lines.append({"period": c["period"], "kind": c["kind"], "gross": to_rupees(c["gross"]),
                      "concession": to_rupees(c["concession"]), "amount": to_rupees(c["amount"]),
                      "paid": to_rupees(c["paid"]), "status": status, "due_date": c["due_date"].isoformat(),
                      "days_overdue": max(0, (today - c["due_date"]).days) if unpaid else 0})
    return {
        "member": member,
        "charges": lines,
        "payments": [{"utr": p["utr"], "channel": p["channel"], "amount": to_rupees(p["amount"]),
                      "received_at": p["received_at"].astimezone(IST).isoformat(),
                      "matched_by": p["match_rule"]}
                     for p in payments],
        "totals": {k: to_rupees(v) for k, v in totals.items()},
    }


def collection_report(conn, inst: int, period: str) -> dict:
    """One month: what was billed, what came in against it, and who still owes."""
    check_period(period)
    with tenant_tx(conn, inst):
        rows = conn.execute(
            """SELECT m.id, m.ref_code, m.name, SUM(c.amount) AS billed,
                      SUM(COALESCE(a.paid, 0)) AS collected
                 FROM charges c JOIN members m ON m.id = c.member_id
                 LEFT JOIN (SELECT charge_id, SUM(amount) AS paid FROM allocations GROUP BY charge_id) a
                        ON a.charge_id = c.id
                WHERE c.period = %s
                GROUP BY m.id ORDER BY m.id""", (period,)).fetchall()
        waiting = conn.execute("SELECT COUNT(*) AS n FROM bank_credits WHERE status = 'needs_review'"
                               ).fetchone()["n"]
    billed = sum(r["billed"] for r in rows)
    collected = sum(r["collected"] for r in rows)
    owing = [{"member_id": r["id"], "ref_code": r["ref_code"], "name": r["name"],
              "outstanding": to_rupees(r["billed"] - r["collected"])}
             for r in rows if r["billed"] > r["collected"]]
    return {
        "period": period, "billed": to_rupees(billed), "collected": to_rupees(collected),
        "outstanding": to_rupees(billed - collected), "collection_rate_pct": _percent(collected, billed),
        "members_billed": len(rows), "paid_in_full": len(rows) - len(owing), "owing": owing,
        "credits_waiting_for_review": waiting,
    }


_BUCKETS = ("not_yet_due", "days_1_30", "days_31_60", "days_61_90", "over_90")


def ageing(conn, inst: int, as_of: date) -> dict:
    """Unpaid amounts per member by how long they have been overdue. FILTER gives all buckets in one pass."""
    with tenant_tx(conn, inst):
        rows = conn.execute(
            """WITH unpaid AS (
                   SELECT c.member_id, c.due_date, c.amount - COALESCE(SUM(a.amount), 0) AS amount
                     FROM charges c LEFT JOIN allocations a ON a.charge_id = c.id
                    GROUP BY c.id HAVING c.amount - COALESCE(SUM(a.amount), 0) > 0)
               SELECT m.id, m.ref_code, m.name,
                      COALESCE(SUM(u.amount) FILTER (WHERE u.due_date >= %(d)s), 0)                    AS not_yet_due,
                      COALESCE(SUM(u.amount) FILTER (WHERE %(d)s - u.due_date BETWEEN 1 AND 30), 0)  AS days_1_30,
                      COALESCE(SUM(u.amount) FILTER (WHERE %(d)s - u.due_date BETWEEN 31 AND 60), 0) AS days_31_60,
                      COALESCE(SUM(u.amount) FILTER (WHERE %(d)s - u.due_date BETWEEN 61 AND 90), 0) AS days_61_90,
                      COALESCE(SUM(u.amount) FILTER (WHERE %(d)s - u.due_date > 90), 0)               AS over_90,
                      SUM(u.amount) AS total
                 FROM unpaid u JOIN members m ON m.id = u.member_id
                GROUP BY m.id ORDER BY total DESC, m.id""", {"d": as_of}).fetchall()
    totals = {b: sum(r[b] for r in rows) for b in (*_BUCKETS, "total")}
    return {
        "as_of": as_of.isoformat(),
        "members": [{"member_id": r["id"], "ref_code": r["ref_code"], "name": r["name"],
                     **{b: to_rupees(r[b]) for b in (*_BUCKETS, "total")}} for r in rows],
        "totals": {b: to_rupees(v) for b, v in totals.items()},
    }


def defaulters(conn, inst: int, as_of: date, min_days: int = 30,
               limit: int | None = None, offset: int | None = None) -> list[dict]:
    """Members with money overdue at least min_days, largest first.

    Ordered before it is cut, so a page is the worst N rather than an arbitrary
    N -- which is the only cut that means anything for this report.
    """
    size, start = limits(limit, offset)
    with tenant_tx(conn, inst):
        rows = conn.execute(
            """WITH unpaid AS (
                   SELECT c.member_id, c.due_date, c.amount - COALESCE(SUM(a.amount), 0) AS amount
                     FROM charges c LEFT JOIN allocations a ON a.charge_id = c.id
                    GROUP BY c.id HAVING c.amount - COALESCE(SUM(a.amount), 0) > 0)
               SELECT m.id, m.ref_code, m.name, m.phone, SUM(u.amount) AS overdue,
                      MAX(%(d)s - u.due_date) AS oldest_days
                 FROM unpaid u JOIN members m ON m.id = u.member_id
                WHERE %(d)s - u.due_date >= %(min)s
                GROUP BY m.id ORDER BY overdue DESC, m.id
                LIMIT %(limit)s OFFSET %(offset)s""",
            {"d": as_of, "min": min_days, "limit": size, "offset": start}).fetchall()
    return [{"member_id": r["id"], "ref_code": r["ref_code"], "name": r["name"], "phone": r["phone"],
             "overdue": to_rupees(r["overdue"]), "oldest_days_overdue": r["oldest_days"]} for r in rows]
