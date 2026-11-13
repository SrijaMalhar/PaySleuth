"""Tune the suggestion weights against what people actually chose in the review queue.

    python tools/tune_weights.py --institution 1

Reads review_outcomes, reports how often the top suggestion was right with the
current weights, searches for weights that would have done better, and checks
the gain on held-out decisions. It prints a proposal; changing
matching.WEIGHTS is a decision for a person, made with the numbers in front
of them. With fewer than 50 measured decisions it refuses to propose anything.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paysleuth.db import make_pool  # noqa: E402
from paysleuth.evaluation import (MIN_OUTCOMES_TO_TUNE, _pct, cross_validate, load_outcomes,  # noqa: E402
                                  signal_stats, top1_hits, tune)
from paysleuth.matching import WEIGHTS  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--institution", type=int, required=True)
    parser.add_argument("--database-url", default=os.environ.get(
        "PAYSLEUTH_DATABASE_URL", "postgresql://paysleuth:paysleuth@localhost:5432/paysleuth"))
    args = parser.parse_args(argv)

    pool = make_pool(args.database_url, max_size=2)
    try:
        with pool.connection() as conn:
            outcomes = load_outcomes(conn, args.institution)
    finally:
        pool.close()

    n = len(outcomes)
    print(f"{n} review decisions measured")
    if n == 0:
        return 0
    print(f"top suggestion right with current weights: {top1_hits(outcomes, WEIGHTS)}/{n} "
          f"({_pct(top1_hits(outcomes, WEIGHTS), n)}%)\n")
    print(f"{'signal':32} {'weight':>6} {'on chosen':>10} {'on others':>10} {'precision':>10}")
    for name, s in signal_stats(outcomes).items():
        precision = "-" if s["precision_pct"] is None else s["precision_pct"] + "%"
        print(f"{name:32} {s['weight']:>6} {s['fired_on_chosen']:>10} {s['fired_on_others']:>10} {precision:>10}")

    if n < MIN_OUTCOMES_TO_TUNE:
        print(f"\nNot proposing new weights: {n} decisions is too few to trust "
              f"(need at least {MIN_OUTCOMES_TO_TUNE}).")
        return 0
    weights, hits = tune(outcomes)
    held_out = cross_validate(outcomes)
    print(f"\nbest weights found: {hits}/{n} ({_pct(hits, n)}%) on the decisions they were fitted to")
    print(f"5-fold check on held-out decisions: current {held_out['current_top1_pct']}%, "
          f"tuned {held_out['tuned_top1_pct']}%")
    changed = {k: (WEIGHTS[k], v) for k, v in weights.items() if v != WEIGHTS[k]}
    if not changed:
        print("no change proposed: the current weights are already the best found")
    for name, (old, new) in changed.items():
        print(f"  {name}: {old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
