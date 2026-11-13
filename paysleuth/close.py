"""The daily close: does everything add up, and is anything missing?

Three questions, asked every day:

    1. Does each statement follow on from the one before it?   (statement_imports.follows)
    2. Does each statement's own running balance hold?         (statement_imports.continuity)
    3. Is every rupee the bank credited either applied to a bill, held as an
       advance, waiting for review, or returned by the bank?

If any answer is no, an alert is raised for a person to look at.
"""

import hashlib
from datetime import date

from psycopg.types.json import Jsonb

from .alerts import raise_alert
from .continuity import Continuity
from .db import tenant_tx
from .money import to_rupees


def record_statement(conn, inst: int, text: str, check: Continuity, line_count: int,
                     account: str = "main") -> dict:
    """Remember what a statement said about itself and compare it with the previous one."""
    digest = hashlib.sha256(text.encode()).hexdigest()
    with tenant_tx(conn, inst):
        seen = conn.execute("SELECT * FROM statement_imports WHERE file_sha256 = %s", (digest,)).fetchone()
        if seen:
            return _statement_view(seen, already_imported=True)

        follows, gap = "unknown", None
        if check.status != "no_balance" and check.first_at is not None:
            previous = conn.execute(
                """SELECT id, closing_balance, last_at FROM statement_imports
                    WHERE account = %s AND closing_balance IS NOT NULL AND last_at <= %s
                    ORDER BY last_at DESC, id DESC LIMIT 1""", (account, check.first_at)).fetchone()
            if previous is None:
                any_before = conn.execute("SELECT 1 FROM statement_imports WHERE account = %s "
                                          "AND closing_balance IS NOT NULL LIMIT 1", (account,)).fetchone()
                follows = "overlap" if any_before else "first"
            elif previous["closing_balance"] == check.opening:
                follows = "continues"
            else:
                follows = "gap"
                gap = {"previous_statement_id": previous["id"],
                       "previous_closing": to_rupees(previous["closing_balance"]),
                       "this_opening": to_rupees(check.opening),
                       "unaccounted": to_rupees(check.opening - previous["closing_balance"])}

        row = conn.execute(
            """INSERT INTO statement_imports (institution_id, account, file_sha256, first_at, last_at,
                   opening_balance, closing_balance, total_credits, total_debits, line_count,
                   continuity, follows, breaks)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (inst, account, digest, check.first_at, check.last_at, check.opening, check.closing,
             check.total_credits, check.total_debits, line_count, check.status, follows,
             Jsonb(check.breaks))).fetchone()
        if check.status == "broken":
            raise_alert(conn, inst, "statement_broken",
                        f"Statement {row['id']}: the running balance breaks at line "
                        f"{check.breaks[0]['line']}; a transaction is missing or changed",
                        f"statement:{row['id']}:broken", statement_id=row["id"], breaks=check.breaks)
        if follows == "gap":
            raise_alert(conn, inst, "statement_gap",
                        f"Statement {row['id']} opens at Rs {gap['this_opening']} but the previous one "
                        f"closed at Rs {gap['previous_closing']}; a page or a day is missing",
                        f"statement:{row['id']}:gap", statement_id=row["id"], **gap)
        return _statement_view(row, already_imported=False, gap=gap)


def _money(value: int | None) -> str | None:
    return None if value is None else to_rupees(value)


def _statement_view(row: dict, *, already_imported: bool, gap: dict | None = None) -> dict:
    return {"id": row["id"], "already_imported": already_imported, "continuity": row["continuity"],
            "follows": row["follows"], "opening_balance": _money(row["opening_balance"]),
            "closing_balance": _money(row["closing_balance"]),
            "total_credits": to_rupees(row["total_credits"]), "total_debits": to_rupees(row["total_debits"]),
            "breaks": row["breaks"], "gap": gap}


def list_statements(conn, inst: int) -> list[dict]:
    with tenant_tx(conn, inst):
        rows = conn.execute("SELECT * FROM statement_imports ORDER BY id").fetchall()
    return [_statement_view(r, already_imported=True) for r in rows]


def run_close(conn, inst: int, day: date) -> dict:
    """Account for every credited rupee and check the statements, then store the result for the day."""
    with tenant_tx(conn, inst):
        # One statement, so every sum comes from the same snapshot even while payments land.
        f = conn.execute(
            """SELECT
                 COALESCE((SELECT SUM(amount) FROM bank_credits), 0)                                AS credited,
                 COALESCE((SELECT SUM(amount) FROM allocations), 0)                                 AS applied,
                 COALESCE((SELECT SUM(amount) FROM payments), 0)                                    AS paid,
                 COALESCE((SELECT SUM(amount) FROM bank_credits WHERE status = 'needs_review'), 0)  AS waiting,
                 COALESCE((SELECT SUM(amount) FROM bank_credits WHERE status = 'reversed'), 0)      AS reversed,
                 (SELECT COUNT(*) FROM bank_credits)                                                AS credits,
                 (SELECT COUNT(*) FROM alerts WHERE status = 'open'
                     AND kind IN ('statement_broken', 'statement_gap'))                             AS statement_alerts
            """).fetchone()
        advance = f["paid"] - f["applied"]
        unexplained = f["credited"] - f["applied"] - advance - f["waiting"] - f["reversed"]
        mismatched = []
        if unexplained != 0:
            mismatched = conn.execute(
                """SELECT b.id, b.utr, b.status, b.amount, COALESCE(SUM(p.amount), 0) AS applied
                     FROM bank_credits b LEFT JOIN payments p ON p.bank_credit_id = b.id
                    GROUP BY b.id
                   HAVING COALESCE(SUM(p.amount), 0) <> CASE WHEN b.status = 'matched' THEN b.amount ELSE 0 END
                    ORDER BY b.id LIMIT 20""").fetchall()
        figures = {
            "credits": f["credits"],
            "credited": to_rupees(f["credited"]),
            "applied_to_bills": to_rupees(f["applied"]),
            "held_as_advance": to_rupees(advance),
            "waiting_for_review": to_rupees(f["waiting"]),
            "returned_by_bank": to_rupees(f["reversed"]),
            "unexplained": to_rupees(unexplained),
            "books_balance": unexplained == 0,
            "statements_complete": f["statement_alerts"] == 0,
            "open_statement_alerts": f["statement_alerts"],
            "mismatched_credits": [{"id": m["id"], "utr": m["utr"], "status": m["status"],
                                    "amount": to_rupees(m["amount"]), "applied": to_rupees(m["applied"])}
                                   for m in mismatched],
        }
        status = "clean" if figures["books_balance"] and figures["statements_complete"] else "attention"
        conn.execute(
            """INSERT INTO daily_closes (institution_id, day, status, figures) VALUES (%s, %s, %s, %s)
               ON CONFLICT (institution_id, day)
               DO UPDATE SET status = EXCLUDED.status, figures = EXCLUDED.figures, created_at = now()""",
            (inst, day, status, Jsonb(figures)))
        if unexplained != 0:
            raise_alert(conn, inst, "books_unbalanced",
                        f"Daily close {day.isoformat()}: Rs {to_rupees(unexplained)} credited by the bank "
                        f"is not applied, held, waiting or returned",
                        f"close:{day.isoformat()}:unbalanced", day=day.isoformat(),
                        mismatched_credits=figures["mismatched_credits"])
        return {"day": day.isoformat(), "status": status, **figures}


def recent_closes(conn, inst: int, limit: int = 30) -> list[dict]:
    with tenant_tx(conn, inst):
        rows = conn.execute("SELECT day, status, figures FROM daily_closes ORDER BY day DESC LIMIT %s",
                            (limit,)).fetchall()
    return [{"day": r["day"].isoformat(), "status": r["status"], **r["figures"]} for r in rows]
