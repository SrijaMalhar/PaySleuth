"""Count the queries and time one credit takes, before and after a change.

    python bench/query_count.py

Wraps the connection's execute to count statements, then records one credit of
each kind. Prints the counts and the median wall time per credit, so a
performance change can be quoted with numbers instead of a claim.
"""

import random
import statistics
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from conftest import fresh_pool  # noqa: E402
from factory import build_institution  # noqa: E402

from paysleuth.charges import run_billing  # noqa: E402
from paysleuth.institutions import create_institution  # noqa: E402
from paysleuth.payments import record_credit  # noqa: E402
from paysleuth.periods import IST  # noqa: E402
from paysleuth.statements import Credit  # noqa: E402


class Counting:
    """Counts execute() calls on one connection, grouped by the statement's first words."""

    def __init__(self, conn):
        self.conn = conn
        self.counts = Counter()
        self._real = conn.execute

    def __enter__(self):
        def execute(sql, params=None, **kwargs):
            text = sql if isinstance(sql, str) else sql.decode()
            self.counts[" ".join(text.split())[:60]] += 1
            return self._real(sql, params, **kwargs)
        self.conn.execute = execute
        return self

    def __exit__(self, *exc):
        self.conn.execute = self._real

    @property
    def total(self):
        return sum(self.counts.values())


def measure(label, make_credit, *, repeats=40, members=200):
    rng = random.Random(7)
    with fresh_pool(max_size=4) as pool, pool.connection() as conn:
        inst, _ = create_institution(conn, "Benchmark Centre")
        families = build_institution(conn, inst, rng, pairs=members // 4, singles=members // 2,
                                     bill_through=date(2026, 7, 1))
        run_billing(conn, inst, date(2026, 7, 1))

        first, times = None, []
        for n in range(repeats):
            credit = make_credit(n, families, rng)
            started = time.perf_counter()
            with Counting(conn) as counting:
                record_credit(conn, inst, credit)
            times.append((time.perf_counter() - started) * 1000)
            if first is None:
                first = counting.counts
        return {"label": label, "queries": sum(first.values()), "top": first.most_common(6),
                "median_ms": statistics.median(times), "slowest_ms": max(times)}


def _credit(n, note=None, upi=None, amount=200000):
    return Credit(utr=f"80{n:010d}", amount=amount, received_at=datetime(2026, 7, 10, tzinfo=IST),
                  note=note, payer_upi_id=upi)


CASES = [
    ("one member, by reference code",
     lambda n, families, rng: _credit(n, note=rng.choice(
         [m[1] for f in families if len(f["members"]) == 1 for m in f["members"]]))),
    ("two siblings, split by dues",
     lambda n, families, rng: (lambda f: _credit(
         n, upi=f["upi"], amount=sum(m[2] for m in f["members"])))(
             rng.choice([f for f in families if len(f["members"]) == 2]))),
    ("unknown payer, straight to review",
     lambda n, families, rng: _credit(n, upi=f"stranger{n}@ybl")),
]


def main() -> int:
    print(f"{'case':34} {'queries':>8} {'median ms':>10} {'slowest ms':>11}")
    results = [measure(label, make) for label, make in CASES]
    for r in results:
        print(f"{r['label']:34} {r['queries']:>8} {r['median_ms']:>10.1f} {r['slowest_ms']:>11.1f}")
    print("\nqueries in the first credit of each case:")
    for r in results:
        print(f"\n  {r['label']}")
        for sql, count in r["top"]:
            print(f"    {count:>3}x {sql}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
