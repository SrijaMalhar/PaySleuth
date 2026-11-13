"""Command line for setup tasks.

    python -m paysleuth.cli migrate
    python -m paysleuth.cli create-institution "Bright Minds Tuition" --late-fee 100 --grace-days 5
"""

import argparse
import os
import sys

from .db import make_pool, migrate
from .institutions import create_institution
from .money import to_paise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="paysleuth")
    parser.add_argument("--database-url", default=os.environ.get(
        "PAYSLEUTH_DATABASE_URL", "postgresql://paysleuth:paysleuth@localhost:5432/paysleuth"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="create or update the schema")
    create = commands.add_parser("create-institution", help="add an institution and print its API key once")
    create.add_argument("name")
    create.add_argument("--late-fee", default="0", help="late fee in rupees, e.g. 100")
    create.add_argument("--grace-days", type=int, default=0)
    create.add_argument("--quiet-start", type=int, default=21, help="hour (IST) quiet hours begin")
    create.add_argument("--quiet-end", type=int, default=8, help="hour (IST) quiet hours end")
    args = parser.parse_args(argv)

    pool = make_pool(args.database_url, max_size=2)
    try:
        migrate(pool)
        if args.command == "migrate":
            print("schema is up to date")
            return 0
        late_fee = 0 if args.late_fee in ("0", "0.00") else to_paise(args.late_fee)
        with pool.connection() as conn:
            institution_id, key = create_institution(
                conn, args.name, late_fee=late_fee, grace_days=args.grace_days,
                quiet_start=args.quiet_start, quiet_end=args.quiet_end)
        print(f"institution_id={institution_id}")
        print(f"api_key={key}")
        print("Store the key now; it cannot be shown again.", file=sys.stderr)
        return 0
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
