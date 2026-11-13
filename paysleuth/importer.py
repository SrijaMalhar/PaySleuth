"""Importing a whole statement on several connections with the same result as one.

The rule: parallel across members, in statement order within a member.

Handing credits to threads at random would keep the books valid, because every
write takes member locks, but not deterministic: if two of Diya's payments
race, which one pays June depends on timing, and so does a sibling split. So
credits are grouped such that no two groups share a member or a UTR; each group
runs in statement order on one thread, and groups cannot affect each other.

The grouping is a determinism and performance layer, not the safety layer. If
it were wrong, the locks would still keep the books valid; the result would
just depend on timing.
"""

import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from .close import record_statement
from .continuity import check_continuity
from .db import tenant_tx
from .errors import Conflict, NotFound
from .matching import candidate_members
from .members import normalise_upi_id
from .payments import normalise_utr, record_credit, reverse_credit
from .statements import Credit, parse_statement


class _DisjointSet:
    """Union-find with path halving and union by size: near-constant time per operation.

    Dicts rather than arrays because the nodes are tuples, not numbers 0..n.
    """

    def __init__(self):
        self.parent: dict = {}
        self.size: dict = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        self.size.setdefault(x, 1)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]     # path halving
            x = self.parent[x]
        return x

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.size[ra] < self.size[rb]:                    # union by size: small tree under big
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]


def _order(credit: Credit):
    return credit.received_at, credit.line


def _group(pool, inst: int, credits: list[Credit], errors: list[dict]) -> list[list[Credit]]:
    """Bucket credits so that every credit sharing a member or a UTR is in one group.

    Each UTR is a node and each candidate member is a node; a credit's UTR is
    joined to every member it could belong to. A sibling transfer joins both
    siblings, so all of their credits end up together. A repeated line has the
    same UTR node, so its second copy runs after the first and is a clean
    duplicate. Nodes are tagged tuples, so a UTR and a member id never collide.
    """
    sets = _DisjointSet()
    kept = []
    with pool.connection() as conn, tenant_tx(conn, inst):
        for credit in credits:
            try:
                utr = normalise_utr(credit.utr)
                upi_id = normalise_upi_id(credit.payer_upi_id)
            except ValueError as error:
                errors.append({"line": credit.line, "error": str(error)})
                continue
            node = ("utr", utr)
            sets.find(node)
            members, _ = candidate_members(conn, upi_id, credit.note)   # read-only
            for member_id in members:
                sets.union(node, ("member", member_id))
            kept.append((credit, node))

    buckets = defaultdict(list)
    for credit, node in kept:
        buckets[sets.find(node)].append(credit)
    groups = [sorted(bucket, key=_order) for bucket in buckets.values()]
    groups.sort(key=lambda group: _order(group[0]))
    return groups


def _process(pool, inst: int, group: list[Credit]) -> list[tuple]:
    """Record one group's credits in order on this thread's own connection."""
    outcomes = []
    with pool.connection() as conn:
        for credit in group:
            try:
                result = record_credit(conn, inst, credit)
                outcome = "duplicate" if result["duplicate"] else result["credit"]["status"]
                outcomes.append((credit.line, outcome, result["credit"]["utr"]))
            except Conflict as error:
                outcomes.append((credit.line, "conflict", str(error)))
            except (ValueError, NotFound) as error:
                outcomes.append((credit.line, "error", str(error)))
    return outcomes


def _apply_reversals(pool, inst: int, reversals, problems: list[dict]) -> int:
    """Apply the statement's returned credits, after every credit line is in."""
    applied = 0
    with pool.connection() as conn:
        for reversal in sorted(reversals, key=lambda r: (r.reversed_at, r.line)):
            with tenant_tx(conn, inst):
                credit = conn.execute("SELECT id, amount FROM bank_credits WHERE utr = %s",
                                      (reversal.utr,)).fetchone()
            if credit is None:
                problems.append({"line": reversal.line,
                                 "error": f"reversal of UTR {reversal.utr}, which was never recorded"})
            elif credit["amount"] != reversal.amount:
                problems.append({"line": reversal.line,
                                 "error": f"reversal of UTR {reversal.utr} is for a different amount"})
            else:
                result = reverse_credit(conn, inst, credit["id"], reason="returned by the bank",
                                        reference=reversal.narration, reversed_at=reversal.reversed_at)
                applied += not result["duplicate"]
    return applied


def import_credits(pool, inst: int, credits: list[Credit], *, workers: int = 8, errors=None,
                   skipped_debits: int = 0, reversals=()) -> dict:
    """Record many credits in parallel. Give the pool at least workers + 1 connections.

    Threads, not processes: the time is spent waiting on PostgreSQL, and Python
    releases the GIL while a thread waits on the network.
    """
    started = time.perf_counter()
    problems = list(errors or [])
    groups = _group(pool, inst, credits, problems)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        outcomes = [o for batch in executor.map(lambda g: _process(pool, inst, g), groups) for o in batch]

    counts = defaultdict(int)
    for line, outcome, detail in outcomes:
        counts[outcome] += 1
        if outcome in ("conflict", "error"):
            problems.append({"line": line, "error": detail})
    reversed_count = _apply_reversals(pool, inst, reversals, problems)
    elapsed = time.perf_counter() - started
    recorded = counts["matched"] + counts["needs_review"]
    return {
        "recorded": recorded,
        "matched": counts["matched"],
        "needs_review": counts["needs_review"],
        "duplicates": counts["duplicate"],
        "conflicts": counts["conflict"],
        "errors": len(problems) - counts["conflict"],
        "reversals": reversed_count,
        "skipped_debits": skipped_debits,
        "groups": len(groups),
        "problems": sorted(problems, key=lambda p: p["line"]),
        "elapsed_ms": round(elapsed * 1000, 1),
        "credits_per_second": round(len(outcomes) / elapsed, 1) if elapsed > 0 else None,
    }


def import_statement(pool, inst: int, text: str, *, workers: int = 8, account: str = "main") -> dict:
    """Import a statement, and check that it accounts for itself and follows the previous one.

    A statement that fails the check is still imported, because its credit
    lines are real facts, but it is flagged and an alert is raised instead of
    the file being silently trusted.
    """
    parsed = parse_statement(text)
    with pool.connection() as conn:
        statement = record_statement(conn, inst, text, check_continuity(parsed.rows), len(parsed.rows),
                                     account)
    summary = import_credits(pool, inst, parsed.credits, workers=workers, errors=parsed.errors,
                             skipped_debits=parsed.skipped_debits, reversals=parsed.reversals)
    return {**summary, "statement": statement}
