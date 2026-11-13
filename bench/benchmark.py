"""Benchmarks. Run against a local PostgreSQL and quote your own numbers.

    python bench/benchmark.py                 # both benchmarks
    python bench/benchmark.py --credits 1000  # smaller run

1. Import scaling: one statement imported with 1, 2, 4 and 8 workers, each into
   a fresh schema. The fingerprint is a hash of the resulting books and must be
   identical on every row, or parallel import is not equivalent to serial.
2. Lock granularity: 8 threads x 100 transfers, once for 8 different members
   and once all for one member.
"""

import argparse
import hashlib
import json
import os
import platform
import random
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from conftest import fresh_pool  # noqa: E402
from factory import books, build_institution, messy_statement  # noqa: E402

from paysleuth.importer import import_statement  # noqa: E402
from paysleuth.institutions import create_institution  # noqa: E402
from paysleuth.members import create_member  # noqa: E402
from paysleuth.payments import record_credit  # noqa: E402
from paysleuth.periods import IST  # noqa: E402
from paysleuth.statements import Credit  # noqa: E402


def fingerprint(pool, inst) -> str:
    with pool.connection() as conn:
        state = books(conn, inst)
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()[:12]


def import_scaling(members: int, credits: int, seed: int = 42) -> list[dict]:
    rows = []
    for workers in (1, 2, 4, 8):
        with fresh_pool(max_size=workers + 4) as pool:
            rng = random.Random(seed)
            with pool.connection() as conn:
                inst, _ = create_institution(conn, "Benchmark Centre")
                families = build_institution(conn, inst, rng, pairs=members // 4,
                                             singles=members - 2 * (members // 4),
                                             bill_through=date(2026, 7, 1))
            per_round = len(families) * 1.2
            text = messy_statement(families, rng, rounds=max(1, round(credits / per_round)), strangers=50)
            summary = import_statement(pool, inst, text, workers=workers)
            lines = summary["recorded"] + summary["duplicates"] + summary["conflicts"]
            rows.append({"workers": workers, "lines": lines, "seconds": summary["elapsed_ms"] / 1000,
                         "per_second": summary["credits_per_second"], "groups": summary["groups"],
                         "fingerprint": fingerprint(pool, inst)})
    return rows


def lock_granularity(threads: int = 8, transfers: int = 100) -> dict:
    out = {}
    for label, shared in (("different members", False), ("one member", True)):
        with fresh_pool(max_size=threads + 2) as pool:
            with pool.connection() as conn:
                inst, _ = create_institution(conn, "Benchmark Centre")
                ids = [create_member(conn, inst, name=f"Child {i}", monthly_fee=200000, billing_day=5,
                                     start_period="2026-06")["id"] for i in range(threads)]
            barrier = threading.Barrier(threads)

            def work(i):
                target = ids[0] if shared else ids[i]
                with pool.connection() as conn:
                    barrier.wait()
                    for n in range(transfers):
                        record_credit(conn, inst, Credit(
                            utr=f"7{i:02d}{n:09d}", amount=10000, note=f"PS{1000 + target}",
                            received_at=datetime(2026, 6, 10, tzinfo=IST)))

            pool_threads = [threading.Thread(target=work, args=(i,)) for i in range(threads)]
            started = time.perf_counter()
            for t in pool_threads:
                t.start()
            for t in pool_threads:
                t.join()
            out[label] = round(threads * transfers / (time.perf_counter() - started), 1)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--members", type=int, default=500)
    parser.add_argument("--credits", type=int, default=5000)
    args = parser.parse_args()

    print(f"machine: {platform.processor() or platform.machine()}, {os.cpu_count()} cores, "
          f"Python {platform.python_version()}\n")
    rows = import_scaling(args.members, args.credits)
    base = rows[0]["seconds"]
    print(f"import scaling: {rows[0]['lines']} statement lines, {args.members} members, "
          f"{rows[0]['groups']} groups")
    print(f"{'workers':>7} {'seconds':>8} {'lines/s':>8} {'speed-up':>8}  fingerprint")
    for r in rows:
        print(f"{r['workers']:>7} {r['seconds']:>8.1f} {r['per_second']:>8.0f} {base / r['seconds']:>7.2f}x  "
              f"{r['fingerprint']}")
    same = len({r["fingerprint"] for r in rows}) == 1
    print("fingerprints identical" if same else "FINGERPRINTS DIFFER")

    rates = lock_granularity()
    print("\nlock granularity: 8 threads x 100 transfers")
    for label, rate in rates.items():
        print(f"  {label:18} {rate:>7.1f} transfers/s")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
