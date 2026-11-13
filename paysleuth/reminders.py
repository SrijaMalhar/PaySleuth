"""Overdue reminders on days 1, 5 and 10 after a fee falls due."""

from datetime import date

from . import outbox
from .db import tenant_tx
from .money import to_rupees

STAGES = (1, 5, 10)        # days after the due date


def queue_reminders(conn, inst: int, today: date) -> int:
    """Queue the latest reminder stage each unpaid monthly fee has reached.

    If the job did not run for a week, a member gets one reminder, not three.
    The dedupe key makes running the job any number of times queue each stage
    once. Late fees get no reminders of their own; the next monthly reminder
    covers them. Whether to actually send is re-checked at delivery time.
    """
    queued = 0
    with tenant_tx(conn, inst):
        rows = conn.execute(
            """SELECT c.id, c.member_id, c.period, c.due_date, m.ref_code, m.name, m.phone,
                      c.amount - COALESCE(SUM(a.amount), 0) AS unpaid
                 FROM charges c JOIN members m ON m.id = c.member_id
                 LEFT JOIN allocations a ON a.charge_id = c.id
                WHERE c.kind = 'monthly_fee' AND m.active AND c.due_date < %s
                GROUP BY c.id, m.id
               HAVING c.amount - COALESCE(SUM(a.amount), 0) > 0
                ORDER BY c.id""", (today,)).fetchall()
        for row in rows:
            days = (today - row["due_date"]).days
            stage = max(s for s in STAGES if days >= s)
            message_id = outbox.enqueue(
                conn, inst, kind="overdue_reminder", member_id=row["member_id"], charge_id=row["id"],
                dedupe_key=f"reminder:{row['id']}:{stage}", quiet_hours=True,
                payload={"member_id": row["member_id"], "ref_code": row["ref_code"],
                         "member_name": row["name"], "phone": row["phone"], "period": row["period"],
                         "due_date": row["due_date"].isoformat(), "days_overdue": days, "stage": stage,
                         "amount_due": to_rupees(row["unpaid"]),
                         # Paying with this note matches by rule 1 next time.
                         "pay_with_note": f"{row['ref_code']} {row['period']}"})
            queued += message_id is not None
    return queued
