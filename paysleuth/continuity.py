"""Does a statement account for itself? Noticing when data is missing, not just when it is wrong.

A bank statement prints a running balance. If every line's balance equals the
previous balance plus its credit minus its debit, no line is missing from the
middle of the file. If the opening balance equals the previous statement's
closing balance, no page or day is missing between the two files.

Pure Python: nothing here touches the database.
"""

from dataclasses import dataclass, field
from datetime import datetime

from .money import to_rupees
from .statements import StatementRow


@dataclass
class Continuity:
    status: str                              # 'ok', 'broken' or 'no_balance'
    opening: int | None = None
    closing: int | None = None
    total_credits: int = 0
    total_debits: int = 0
    first_at: datetime | None = None
    last_at: datetime | None = None
    newest_first: bool = False
    breaks: list[dict] = field(default_factory=list)
    repeated_lines: list[int] = field(default_factory=list)


def _same(a: StatementRow, b: StatementRow) -> bool:
    return (a.at, a.debit, a.credit, a.balance, a.narration) == (b.at, b.debit, b.credit, b.balance, b.narration)


def _breaks(rows: list[StatementRow]) -> list[dict]:
    found = []
    for previous, row in zip(rows, rows[1:]):
        if row.balance is None or previous.balance is None:
            if row.balance is None:
                found.append({"line": row.line, "problem": "no balance printed on this line"})
            continue
        expected = previous.balance + row.credit - row.debit
        if expected != row.balance:
            found.append({"line": row.line, "expected_balance": to_rupees(expected),
                          "printed_balance": to_rupees(row.balance),
                          "unaccounted": to_rupees(row.balance - expected),
                          "problem": "balance does not follow from the line before; "
                                     "a transaction is missing or changed"})
    return found


def check_continuity(rows: list[StatementRow]) -> Continuity:
    """Check that closing = opening + credits - debits holds on every line.

    A line the bank printed twice, identical in every field including the
    balance, is counted once. Banks export oldest-first or newest-first, so
    both directions are tried and the one that fits is used.
    """
    kept, repeated = [], []
    for row in rows:
        if kept and _same(kept[-1], row):
            repeated.append(row.line)
        else:
            kept.append(row)
    if not kept or all(r.balance is None for r in kept):
        return Continuity(status="no_balance", repeated_lines=repeated,
                          total_credits=sum(r.credit for r in kept), total_debits=sum(r.debit for r in kept),
                          first_at=min((r.at for r in kept), default=None),
                          last_at=max((r.at for r in kept), default=None))

    forward, backward = _breaks(kept), _breaks(kept[::-1])
    newest_first = len(backward) < len(forward)
    ordered = kept[::-1] if newest_first else kept
    breaks = backward if newest_first else forward
    first = ordered[0]
    opening = None if first.balance is None else first.balance - first.credit + first.debit
    return Continuity(
        status="broken" if breaks else "ok", opening=opening, closing=ordered[-1].balance,
        total_credits=sum(r.credit for r in kept), total_debits=sum(r.debit for r in kept),
        first_at=min(r.at for r in kept), last_at=max(r.at for r in kept),
        newest_first=newest_first, breaks=breaks, repeated_lines=repeated)
