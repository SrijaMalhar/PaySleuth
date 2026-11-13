"""Print the measured matching numbers for one institution.

    python tools/evaluate.py --institution 1

These are the numbers to quote: the share of credits matched without a person,
how often the top suggestion was the one chosen, and what payers actually send
in the note. They come from that institution's own recorded data; nothing is
estimated.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paysleuth.db import make_pool  # noqa: E402
from paysleuth.evaluation import matching_report  # noqa: E402
from paysleuth.periods import utc_now  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--institution", type=int, required=True)
    parser.add_argument("--database-url", default=os.environ.get(
        "PAYSLEUTH_DATABASE_URL", "postgresql://paysleuth:paysleuth@localhost:5432/paysleuth"))
    args = parser.parse_args(argv)
    pool = make_pool(args.database_url, max_size=2)
    try:
        with pool.connection() as conn:
            print(json.dumps(matching_report(conn, args.institution, utc_now()), indent=2))
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
