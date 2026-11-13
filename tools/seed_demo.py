"""Seed a demo institution: five students, a month billed, a statement imported.

    python -m tools.seed_demo

Leaves the database in the state a tuition centre is in halfway through a
month: fees billed, most payments matched, two credits waiting for a person,
receipts queued. Prints an owner key and a clerk key.

Safe to run twice: it creates a fresh institution each time and the statement
import is idempotent within one, so nothing is double-counted.
"""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paysleuth.charges import apply_late_fees, run_billing  # noqa: E402
from paysleuth.close import run_close  # noqa: E402
from paysleuth.db import make_pool, migrate  # noqa: E402
from paysleuth.importer import import_statement  # noqa: E402
from paysleuth.institutions import add_staff, create_institution  # noqa: E402
from paysleuth.members import create_member  # noqa: E402
from paysleuth.money import to_paise  # noqa: E402
from paysleuth.reminders import queue_reminders  # noqa: E402

STUDENTS = [
    # name, guardian, fee, concession %, UPI IDs
    ("Diya Sharma", "Sunita Sharma", "2000", 0, ["sunita.sharma@oksbi"]),
    ("Rohan Verma", "Rajesh Kumar Verma", "2000", 0, ["rk.verma@okhdfcbank"]),
    ("Kabir Verma", "Rajesh Kumar Verma", "1500", 0, ["rk.verma@okhdfcbank"]),
    ("Arjun Nair", "Anil Nair", "1800", 0, []),
    ("Meera Iyer", "Lakshmi Iyer", "2500", 25, []),
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.environ.get(
        "PAYSLEUTH_DATABASE_URL", "postgresql://paysleuth:paysleuth@localhost:5432/paysleuth"))
    parser.add_argument("--name", default="Bright Minds Tuition")
    parser.add_argument("--as-of", default="2026-06-20", help="the date to bill and age dues against")
    args = parser.parse_args(argv)
    as_of = date.fromisoformat(args.as_of)

    pool = make_pool(args.database_url, max_size=6)
    try:
        migrate(pool)
        with pool.connection() as conn:
            inst, owner_key = create_institution(
                conn, args.name, late_fee=to_paise("100"), grace_days=5,
                approval_threshold=to_paise("5000"), owner_name="Asha (owner)")
            clerk = add_staff(conn, inst, "Chetan (clerk)", "clerk")
            codes = {}
            for name, guardian, fee, concession, upi_ids in STUDENTS:
                view = create_member(conn, inst, name=name, guardian_name=guardian,
                                     monthly_fee=to_paise(fee), billing_day=5, start_period="2026-05",
                                     concession_pct=concession, upi_ids=upi_ids)
                codes[name] = view["ref_code"]
            billed = run_billing(conn, inst, as_of)
            late = apply_late_fees(conn, inst, as_of)

        # The sample statement was written with PS1004 in one note. Reference codes
        # follow the member id, so on a database that already holds an institution
        # the codes differ; point that note at whoever Arjun actually is.
        statement = (ROOT / "sample_data" / "june_2026_statement.csv").read_text()
        statement = statement.replace("PS1004", codes["Arjun Nair"])
        summary = import_statement(pool, inst, statement)

        with pool.connection() as conn:
            reminders = queue_reminders(conn, inst, as_of)
            closed = run_close(conn, inst, as_of)
    finally:
        pool.close()

    print(f"institution_id={inst}")
    print(f"owner_api_key={owner_key}")
    print(f"clerk_api_key={clerk['api_key']}")
    print(f"\nbilled {billed['charges_created']} charges for {billed['members']} students, "
          f"{len(late)} late fees")
    print(f"imported {summary['recorded']} credits: {summary['matched']} matched automatically, "
          f"{summary['needs_review']} waiting for a person, {summary['duplicates']} duplicates, "
          f"{summary['skipped_debits']} debits skipped")
    print(f"statement continuity: {summary['statement']['continuity']}, "
          f"follows: {summary['statement']['follows']}")
    print(f"queued {reminders} reminders; daily close: {closed['status']}")
    print("\nTry:  curl -H \"X-API-Key: <owner key>\" <base url>/review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
