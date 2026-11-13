"""Run one real institution's statement through PaySleuth and measure how it did.

This is the tool for a pilot: an owner gives you their member list and a month
or two of statement, and you want the honest numbers (what matched without a
person, how often the top suggestion was the one chosen) rather than numbers
from synthetic data.

    python -m tools.pilot setup     --name "Sunrise PG" --members members.csv
    python -m tools.pilot import    --institution 1 --statement june.csv
    python -m tools.pilot review    --institution 1
    python -m tools.pilot measure   --institution 1

members.csv needs a header row; see sample_data/members_template.csv.
Only name, monthly_fee and start_period are required.

    name,guardian_name,monthly_fee,phone,upi_ids,billing_day,start_period,concession_pct
    Diya Sharma,Sunita Sharma,2000,9876543210,sunita.sharma@oksbi,5,2026-05,0

A member can have several UPI IDs, separated by spaces or semicolons. Siblings
share a parent's UPI ID by listing the same one.

ON REAL DATA: run this against a local database, measure, then drop the
database. Do not anonymise first if you intend to measure, because the payer
names and UPI IDs are exactly what matching is scored on, and replacing them
would score the replacements. tools/anonymise_statement.py is for the opposite
job: preparing a file you intend to commit as a test fixture, where only the
layout matters.
"""

import argparse
import csv
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paysleuth.charges import apply_late_fees, run_billing  # noqa: E402
from paysleuth.close import run_close  # noqa: E402
from paysleuth.db import make_pool, migrate  # noqa: E402
from paysleuth.errors import Conflict, NotFound  # noqa: E402
from paysleuth.evaluation import matching_report  # noqa: E402
from paysleuth.importer import import_statement  # noqa: E402
from paysleuth.institutions import create_institution  # noqa: E402
from paysleuth.members import create_member  # noqa: E402
from paysleuth.money import to_paise  # noqa: E402
from paysleuth.payments import assign_credit, review_queue, split_credit  # noqa: E402
from paysleuth.periods import today_ist, utc_now  # noqa: E402

REQUIRED = ("name", "monthly_fee", "start_period")


def _rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                for row in csv.DictReader(handle)]
    if not rows:
        raise SystemExit(f"{path} has a header but no members")
    missing = [column for column in REQUIRED if column not in rows[0]]
    if missing:
        raise SystemExit(f"{path} needs the column(s): {', '.join(missing)}")
    return rows


def setup(pool, args) -> int:
    """Create the institution and its members from a CSV."""
    rows = _rows(Path(args.members))
    with pool.connection() as conn:
        inst, key = create_institution(
            conn, args.name,
            late_fee=to_paise(args.late_fee) if args.late_fee not in ("0", "0.00") else 0,
            grace_days=args.grace_days, owner_name=args.owner_name)
        added = 0
        for line, row in enumerate(rows, start=2):
            try:
                view = create_member(
                    conn, inst, name=row["name"], guardian_name=row.get("guardian_name") or None,
                    monthly_fee=to_paise(row["monthly_fee"]), phone=row.get("phone") or None,
                    billing_day=int(row.get("billing_day") or 5),
                    start_period=row["start_period"],
                    concession_pct=int(row.get("concession_pct") or 0),
                    upi_ids=[u for u in (row.get("upi_ids") or "").replace(";", " ").split() if u])
            except ValueError as error:
                print(f"  line {line}: skipped, {error}", file=sys.stderr)
                continue
            added += 1
            print(f"  {view['ref_code']}  {view['name']}  Rs {view['monthly_fee']}")
    print(f"\ninstitution_id={inst}")
    print(f"api_key={key}")
    print(f"{added} of {len(rows)} members added")
    print("\nGive the owner their members' reference codes: a payer who writes one in the note\n"
          "matches by rule 1, which is the cheapest improvement available to them.")
    return 0


def import_file(pool, args) -> int:
    """Import a statement, bill the months it covers, and report what matched."""
    text = Path(args.statement).read_text(encoding="utf-8-sig")
    as_of = date.fromisoformat(args.as_of) if args.as_of else today_ist()
    with pool.connection() as conn:
        billed = run_billing(conn, args.institution, as_of)
        late = apply_late_fees(conn, args.institution, as_of)
    print(f"billed {billed['charges_created']} charges for {billed['members']} members, "
          f"{len(late)} late fees")

    summary = import_statement(pool, args.institution, text, account=args.account)
    statement = summary["statement"]
    print(f"\nbank layout recognised: {summary['bank'] or 'none, used the generic patterns'}")
    print(f"recorded {summary['recorded']} credits: {summary['matched']} matched automatically, "
          f"{summary['needs_review']} waiting for a person")
    print(f"duplicates {summary['duplicates']}, conflicts {summary['conflicts']}, "
          f"debits skipped {summary['skipped_debits']}, reversals {summary['reversals']}")
    print(f"statement: continuity {statement['continuity']}, follows {statement['follows']}, "
          f"credits recorded Rs {statement['credits_recorded']}, dropped Rs {statement['credits_dropped']}")
    if statement["credits_missing"]:
        print("\nCredit lines the books do not have. These are the ones to take back to the owner:")
        for missing in statement["credits_missing"]:
            print(f"  line {missing['line']}: UTR {missing['utr']} Rs {missing['amount']}")
    if summary["problems"]:
        print("\nlines that could not be imported:")
        for problem in summary["problems"][:20]:
            print(f"  line {problem['line']}: {problem['error']}")
    with pool.connection() as conn:
        closed = run_close(conn, args.institution, as_of)
    print(f"\ndaily close: {closed['status']}")
    return 0


def review(pool, args) -> int:
    """Work through the review queue the way the owner would.

    Every decision is recorded with the suggestions that were on screen at the
    time, which is what `measure` later scores the weights against. Guessing
    here poisons that, so skip anything you are not sure of.
    """
    as_of = date.fromisoformat(args.as_of) if args.as_of else today_ist()
    resolved = skipped = 0
    with pool.connection() as conn:
        # One page, walked in Python. Re-reading the queue after each decision
        # would keep showing a skipped credit, since nothing marks it as skipped.
        queue = review_queue(conn, args.institution, as_of, limit=args.limit)
        print(f"{len(queue)} credit(s) waiting\n")
        for position, credit in enumerate(queue, start=1):
            print(f"[{position}/{len(queue)}] Rs {credit['amount']} on {credit['received_at'][:10]} "
                  f"from {credit['payer_name'] or 'unnamed'} ({credit['payer_upi_id'] or 'no UPI ID'})")
            print(f"  note: {credit['note'] or '(empty)'}")
            if not credit["suggestions"]:
                print("  no suggestion")
            for n, suggestion in enumerate(credit["suggestions"], start=1):
                print(f"  {n}. {suggestion['ref_code']} {suggestion['name']} "
                      f"(score {suggestion['score']}): {'; '.join(suggestion['reasons'])}")
            try:
                answer = input("  number to accept, 'PS1234' for someone else, "
                               "'PS1001 600 PS1002 400' to split, s to skip, q to stop: ").strip()
            except EOFError:
                print("  (no more input)")
                break
            if answer.lower() == "q":
                break
            if not answer or answer.lower() == "s":
                print("  skipped\n")
                skipped += 1
                continue
            try:
                resolved += _apply(conn, args.institution, credit, answer)
                print("  applied\n")
            except (ValueError, KeyError, IndexError, Conflict, NotFound) as error:
                print(f"  not applied: {error}\n")
    print(f"{resolved} resolved, {skipped} skipped this session")
    print("Run `measure` when you are through the queue.")
    return 0


def _codes(conn, inst: int) -> dict:
    from paysleuth.db import tenant_tx

    with tenant_tx(conn, inst):
        return {r["ref_code"]: r["id"] for r in
                conn.execute("SELECT id, ref_code FROM members").fetchall()}


def _apply(conn, inst: int, credit: dict, answer: str) -> int:
    """Apply one review decision. Returns 1 if it was applied."""
    parts = answer.split()
    if len(parts) == 1 and parts[0].isdigit():
        member_id = credit["suggestions"][int(parts[0]) - 1]["member_id"]
        assign_credit(conn, inst, credit["id"], member_id)
        return 1
    codes = _codes(conn, inst)
    if len(parts) == 1:
        assign_credit(conn, inst, credit["id"], codes[parts[0].upper()])
        return 1
    if len(parts) % 2:
        raise ValueError("a split needs a reference code and an amount for each member")
    split_credit(conn, inst, credit["id"],
                 [(codes[parts[i].upper()], to_paise(parts[i + 1])) for i in range(0, len(parts), 2)])
    return 1


def measure(pool, args) -> int:
    """Print the measured numbers. These are the only ones worth quoting."""
    with pool.connection() as conn:
        report = matching_report(conn, args.institution, utc_now())
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    def pct(value):
        return "not measured yet" if value is None else f"{value}%"

    print(f"credits recorded                  {report['credits']}")
    print(f"matched without a person          {report['auto_matched']}  ({pct(report['auto_match_rate_pct'])})")
    print(f"resolved by a person              {report['resolved_by_a_person']}")
    print(f"still waiting                     {report['waiting_for_review']}")
    print(f"by rule                           {report['by_rule']}")
    suggestions = report["suggestions"]
    print(f"\ndecisions measured                {suggestions['decisions_measured']}")
    print(f"top suggestion was the choice     {pct(suggestions['top_suggestion_right_pct'])}")
    print(f"choice was in the top 3           {pct(suggestions['choice_in_top_3_pct'])}")
    print(f"choice was not suggested at all   {suggestions['choice_was_not_suggested']}")
    print("\nwhat payers actually send")
    for key, value in report["what_payers_send"].items():
        print(f"  {key:34} {pct(value)}")
    print("\nsignal precision (how often a signal fired on the member who was chosen)")
    print(f"  {'signal':34} {'weight':>6} {'chosen':>7} {'others':>7} {'precision':>10}")
    for name, stats in report["signals"].items():
        print(f"  {name:34} {stats['weight']:>6} {stats['fired_on_chosen']:>7} "
              f"{stats['fired_on_others']:>7} {pct(stats['precision_pct']):>10}")
    if suggestions["decisions_measured"] >= 50:
        print("\nEnough decisions to tune the weights:")
        print(f"  python tools/tune_weights.py --institution {args.institution}")
    else:
        print(f"\n{50 - suggestions['decisions_measured']} more resolved credits before the weights "
              f"can be tuned on evidence.")
    print("\nQuote these numbers and no others. Say how many credits and how long a period they\n"
          "cover, and that they are one institution's.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tools.pilot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=os.environ.get(
        "PAYSLEUTH_DATABASE_URL", "postgresql://paysleuth:paysleuth@localhost:5432/paysleuth"))
    commands = parser.add_subparsers(dest="command", required=True)

    s = commands.add_parser("setup", help="create the institution and its members from a CSV")
    s.add_argument("--name", required=True)
    s.add_argument("--members", required=True, help="CSV of members")
    s.add_argument("--late-fee", default="0", help="late fee in rupees")
    s.add_argument("--grace-days", type=int, default=5)
    s.add_argument("--owner-name", default="Owner")
    s.set_defaults(run=setup)

    i = commands.add_parser("import", help="import a statement and report what matched")
    i.add_argument("--institution", type=int, required=True)
    i.add_argument("--statement", required=True)
    i.add_argument("--account", default="main")
    i.add_argument("--as-of", help="the date to bill through, default today")
    i.set_defaults(run=import_file)

    r = commands.add_parser("review", help="resolve the review queue, recording each decision")
    r.add_argument("--institution", type=int, required=True)
    r.add_argument("--as-of")
    r.add_argument("--limit", type=int, default=200, help="how many waiting credits to walk")
    r.set_defaults(run=review)

    m = commands.add_parser("measure", help="print the measured matching numbers")
    m.add_argument("--institution", type=int, required=True)
    m.add_argument("--json", action="store_true")
    m.set_defaults(run=measure)

    args = parser.parse_args(argv)
    pool = make_pool(args.database_url, max_size=6)
    try:
        migrate(pool)
        return args.run(pool, args)
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
