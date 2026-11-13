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
from .errors import NotFound
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
                   continuity, follows, breaks, repeated_lines, newest_first)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (inst, account, digest, check.first_at, check.last_at, check.opening, check.closing,
             check.total_credits, check.total_debits, line_count, check.status, follows,
             Jsonb(check.breaks), Jsonb(check.repeated_lines), check.newest_first)).fetchone()
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


def check_credits_landed(conn, inst: int, statement_id: int, credits) -> dict:
    """Check every credit line in a statement against the books, and alert on any that is missing.

    The rule: each distinct (UTR, amount) the statement printed must have a
    bank_credits row with that UTR and that amount. Anything else is money the
    bank says it received that the books do not have.

    This asks the database, rather than trusting the importer's own report of
    what it did, so a line lost to any cause is caught: a narration the parser
    could not read, a UTR it rejected, a UTR that came back with a different
    amount, or a bug in the import itself. A line the bank printed twice is one
    (UTR, amount) pair, so a reprint is neither counted twice nor called
    missing.
    """
    printed = {}
    for credit in credits:
        printed[(credit.utr, credit.amount)] = credit.line
    recorded_total, dropped, missing = 0, 0, []
    with tenant_tx(conn, inst):
        found = set()
        if printed:
            found = {(r["utr"], r["amount"]) for r in conn.execute(
                "SELECT utr, amount FROM bank_credits WHERE utr = ANY(%s)",
                ([utr for utr, _ in printed],)).fetchall()}
        for (utr, amount), line in sorted(printed.items(), key=lambda pair: pair[1]):
            if (utr, amount) in found:
                recorded_total += amount
            else:
                dropped += amount
                missing.append({"line": line, "utr": utr, "amount": to_rupees(amount)})
        row = conn.execute(
            "UPDATE statement_imports SET credits_recorded = %s, credits_dropped = %s "
            "WHERE id = %s RETURNING id, total_credits", (recorded_total, dropped, statement_id)).fetchone()
        if row is None:
            raise NotFound(f"statement {statement_id} not found")
        if missing:
            raise_alert(conn, inst, "credits_missing",
                        f"Statement {statement_id}: Rs {to_rupees(dropped)} it credits is not in the "
                        f"books ({len(missing)} line(s), first at line {missing[0]['line']})",
                        f"statement:{statement_id}:missing", statement_id=statement_id,
                        dropped=to_rupees(dropped), missing=missing[:20])
    return {"credits_recorded": to_rupees(recorded_total), "credits_dropped": to_rupees(dropped),
            "credits_missing": missing[:20]}


def _money(value: int | None) -> str | None:
    return None if value is None else to_rupees(value)


def _statement_view(row: dict, *, already_imported: bool, gap: dict | None = None) -> dict:
    return {"id": row["id"], "already_imported": already_imported, "continuity": row["continuity"],
            "follows": row["follows"], "opening_balance": _money(row["opening_balance"]),
            "closing_balance": _money(row["closing_balance"]),
            "total_credits": to_rupees(row["total_credits"]), "total_debits": to_rupees(row["total_debits"]),
            "credits_recorded": _money(row["credits_recorded"]),
            "credits_dropped": _money(row["credits_dropped"]),
            "repeated_lines": row["repeated_lines"], "newest_first": row["newest_first"],
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
                     AND kind IN ('statement_broken', 'statement_gap', 'credits_missing'))        AS statement_alerts,
                 COALESCE((SELECT SUM(credits_dropped) FROM statement_imports), 0)                  AS dropped
            """).fetchone()
        # paid is every live payment; applied is what those payments have been put
        # against charges. The difference is money held for a member, not yet owed.
        advance = f["paid"] - f["applied"]
        # Three independent checks, not one: that the recorded credits are all
        # accounted for, that no more has been applied than was received, and that
        # no statement line was lost on the way in.
        unexplained = f["credited"] - f["paid"] - f["waiting"] - f["reversed"]
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
            "dropped_by_import": to_rupees(f["dropped"]),
            "applied_to_bills": to_rupees(f["applied"]),
            "held_as_advance": to_rupees(advance),
            "waiting_for_review": to_rupees(f["waiting"]),
            "returned_by_bank": to_rupees(f["reversed"]),
            "unexplained": to_rupees(unexplained),
            "books_balance": unexplained == 0 and advance >= 0,
            "every_statement_line_imported": f["dropped"] == 0,
            "statements_complete": f["statement_alerts"] == 0,
            "open_statement_alerts": f["statement_alerts"],
            "mismatched_credits": [{"id": m["id"], "utr": m["utr"], "status": m["status"],
                                    "amount": to_rupees(m["amount"]), "applied": to_rupees(m["applied"])}
                                   for m in mismatched],
        }
        status = ("clean" if figures["books_balance"] and figures["statements_complete"]
                  and figures["every_statement_line_imported"] else "attention")
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
