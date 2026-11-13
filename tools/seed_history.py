"""Seed several months of synthetic history, for a demo with something behind it.

    python -m tools.seed_history --months 4 --members 40

seed_demo.py puts five students and one statement in, which is enough to click
through /docs. This puts in a centre that has been running for a few months:
members who joined partway, siblings on one parent's UPI ID, a scholarship, a
fee that changed, statements in four banks' layouts, partial payments, a
reversal, duplicate lines, credits nobody could match, and the review decisions
a person made about them.

THE DATA IS INVENTED. Every name, UPI ID and UTR here was written by me. The
point is to exercise the reports, the queue and the close against realistic
*shapes* and volumes -- not to produce a number. /reports/matching and
tools/tune_weights.py will both happily compute a rate from this, and that rate
measures this generator, not PaySleuth. The only figures worth quoting come
from tools/pilot.py run on one real institution's own statement.

Reproducible: the generator is seeded, so the same arguments give the same
books. Safe to re-run -- billing, import and assignment are all idempotent, so
a second run against the same database changes nothing.
"""

import argparse
import os
import random
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paysleuth.charges import apply_late_fees, run_billing          # noqa: E402
from paysleuth.close import run_close                                # noqa: E402
from paysleuth.db import make_pool, migrate, tenant_tx               # noqa: E402
from paysleuth.importer import import_statement                      # noqa: E402
from paysleuth.institutions import create_institution                # noqa: E402
from paysleuth.members import create_member, get_member, update_member  # noqa: E402
from paysleuth.money import to_paise, to_rupees                      # noqa: E402
from paysleuth.payments import assign_credit, reverse_credit, review_queue  # noqa: E402
from paysleuth.reminders import queue_reminders                      # noqa: E402

FIRST = ["Diya", "Rohan", "Kabir", "Arjun", "Meera", "Aarav", "Ananya", "Vihaan", "Ishaan",
         "Saanvi", "Advait", "Myra", "Reyansh", "Aadhya", "Kiaan", "Navya", "Shaurya", "Pari",
         "Atharv", "Anika", "Krish", "Riya", "Dhruv", "Tara", "Ved", "Sia", "Yash", "Nitya",
         "Aryan", "Kavya", "Rudra", "Ira", "Veer", "Mishka", "Arnav", "Zara", "Om", "Prisha",
         "Laksh", "Inaya"]
SURNAMES = ["Sharma", "Verma", "Nair", "Iyer", "Reddy", "Patel", "Gupta", "Menon", "Rao",
            "Khanna", "Bose", "Pillai", "Chawla", "Joshi", "Shetty", "Kulkarni"]
GUARDIAN_FIRST = ["Sunita", "Rajesh Kumar", "Anil", "Lakshmi", "Prakash", "Deepa", "Mahesh",
                  "Kavitha", "Suresh", "Anita", "Ramesh", "Shalini", "Vinod", "Geeta"]
HANDLES = ["oksbi", "okhdfcbank", "okicici", "okaxis", "ybl", "paytm"]
STRANGERS = [("GREEN LEAF CAFE", "greenleaf@oksbi", "rent", "12000"),
             ("BHARAT STATIONERS", "bharatstat@okicici", "supplies", "4250"),
             ("S KUMAR", "skumar1982@ybl", "", "3000"),
             ("CLEAN PRO SERVICES", "cleanpro@okaxis", "june cleaning", "2200")]

# The four layouts, each written the way that bank actually prints a UPI credit.
LAYOUTS = {
    "HDFC": {
        "header": "Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance",
        "credit": lambda d, n, u, utr, note, amt, bal:
            f'{d},UPI-{n.upper()}-{u.upper()}-SBIN0001234-{utr}-{note.upper()},{utr},{d},,"{amt}","{bal}"',
        "debit": lambda d, what, amt, bal: f'{d},{what},,{d},"{amt}",,"{bal}"',
    },
    "SBI": {
        "header": "Txn Date,Value Date,Description,Ref No./Cheque No.,Debit,Credit,Balance",
        # SBI truncates the payer name and the UPI ID, which is the whole point of having it here.
        "credit": lambda d, n, u, utr, note, amt, bal:
            f'{d},{d},BY TRANSFER-UPI/CR/{utr}/{n.upper()[:9]}/SBIN/{u[:11]}/{note}--,{utr},,"{amt}","{bal}"',
        "debit": lambda d, what, amt, bal: f'{d},{d},{what},,"{amt}",,"{bal}"',
    },
    "ICICI": {
        "header": "S No.,Value Date,Transaction Date,Cheque Number,Transaction Remarks,"
                  "Withdrawal Amount (INR ),Deposit Amount (INR ),Balance (INR )",
        # ICICI prints the note and the full UPI ID but never the payer's name, so
        # these match on a registered ID or not at all.
        "credit": lambda d, n, u, utr, note, amt, bal:
            f'1,{d},{d},,UPI/{utr}/{note}/{u}/State Bank Of India,,"{amt}","{bal}"',
        "debit": lambda d, what, amt, bal: f'1,{d},{d},,{what},"{amt}",,"{bal}"',
    },
    "AXIS": {
        "header": "Tran Date,CHQNO,PARTICULARS,DR,CR,BAL,SOL",
        # Axis prints the payer's name but no UPI ID at all, so these match on a
        # reference code in the note, or go to review with name suggestions.
        "credit": lambda d, n, u, utr, note, amt, bal:
            f'{d},,UPI/P2A/{utr}/{n.upper()}/State Bank Of India/{note},,"{amt}","{bal}",MGRD',
        "debit": lambda d, what, amt, bal: f'{d},,{what},"{amt}",,"{bal}",MGRD',
    },
}


def shift(period: str, months: int) -> str:
    """The month `months` after `period`, which may be negative."""
    year, month = (int(part) for part in period.split("-"))
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def rupees(paise):
    return f"{paise // 100:,}.{paise % 100:02d}"


class Statement:
    """Builds one month's export in one bank's layout, keeping the balance honest."""

    def __init__(self, bank, opening):
        self.spec = LAYOUTS[bank]
        self.balance = opening
        self.lines = [self.spec["header"]]

    def credit(self, day, name, upi_id, utr, note, paise):
        self.balance += paise
        self.lines.append(self.spec["credit"](day.strftime("%d/%m/%Y"), name, upi_id or "", utr,
                                              note, rupees(paise), rupees(self.balance)))

    def debit(self, day, what, paise):
        self.balance -= paise
        self.lines.append(self.spec["debit"](day.strftime("%d/%m/%Y"), what,
                                             rupees(paise), rupees(self.balance)))

    def text(self):
        return "\n".join(self.lines) + "\n"


def build_members(rng, count, first_period, months):
    """Members, some sharing a guardian's UPI ID as siblings, some joining late."""
    people, families = [], []
    while len(people) < count:
        surname = rng.choice(SURNAMES)
        guardian = f"{rng.choice(GUARDIAN_FIRST)} {surname}"
        handle = rng.choice(HANDLES)
        upi = f"{guardian.split()[0].lower()}.{surname.lower()}@{handle}"
        size = rng.choices([1, 1, 1, 2, 2, 3], k=1)[0]
        size = min(size, count - len(people))
        # Every family has a UPI ID and the bank prints it. What varies is whether the
        # centre ever recorded which member it belongs to: an unregistered ID is the
        # second case in the README -- rank it, explain it, never apply it -- not a line
        # the parser cannot read.
        registered = [upi] if rng.random() > 0.25 else []
        joined = first_period if rng.random() > 0.2 else shift(
            first_period, rng.randrange(1, max(2, months)))
        family = []
        for _ in range(size):
            family.append({
                "name": f"{rng.choice(FIRST)} {surname}",
                "guardian_name": guardian,
                "monthly_fee": rng.choice([1500, 1800, 2000, 2500, 3000, 3500]),
                "phone": f"9{rng.randrange(100000000, 999999999)}",
                "concession_pct": rng.choice([0, 0, 0, 0, 0, 25, 50]),
                "upi_ids": registered,
                "start_period": joined,
                "payer": guardian,
                "payer_upi": upi,          # printed by the bank whether or not it is registered
            })
        people.extend(family)
        families.append(family)
    return people[:count], families


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--database-url", default=os.environ.get(
        "PAYSLEUTH_DATABASE_URL", "postgresql://paysleuth:paysleuth@localhost:5432/paysleuth"))
    parser.add_argument("--members", type=int, default=40)
    parser.add_argument("--months", type=int, default=4)
    parser.add_argument("--name", default="Bright Minds Tuition (synthetic demo data)")
    parser.add_argument("--through", default="2026-06", help="the last billed month, YYYY-MM")
    parser.add_argument("--seed", type=int, default=20260614)
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    last_year, last_month = (int(p) for p in args.through.split("-"))
    first_period = shift(args.through, -(args.months - 1))

    pool = make_pool(args.database_url, max_size=8)
    migrate(pool)
    with pool.connection() as conn:
        inst, owner_key = create_institution(
            conn, args.name, late_fee=to_paise("100"), grace_days=5,
            approval_threshold=to_paise("5000"), owner_name="Asha (owner)")
        print(f"institution_id={inst}")
        print(f"owner_api_key={owner_key}")

        people, families = build_members(rng, args.members, first_period, args.months)
        for person in people:
            view = create_member(conn, inst, name=person["name"], guardian_name=person["guardian_name"],
                                 monthly_fee=to_paise(str(person["monthly_fee"])), billing_day=5,
                                 start_period=person["start_period"], phone=person["phone"],
                                 concession_pct=person["concession_pct"], upi_ids=person["upi_ids"])
            person["id"], person["ref_code"] = view["id"], view["ref_code"]
        print(f"{len(people)} members in {len(families)} families, "
              f"{sum(1 for p in people if p['concession_pct'])} on a concession, "
              f"{sum(1 for f in families if not f[0]['upi_ids'])} families with no UPI ID on file")

        balance = 40_000_00
        totals = {"recorded": 0, "matched": 0, "needs_review": 0, "resolved": 0, "reversed": 0}
        for step in range(args.months):
            period = shift(first_period, step)
            year, month = (int(p) for p in period.split("-"))
            close_day = date(year, month, 28)
            bank = list(LAYOUTS)[step % len(LAYOUTS)]

            billed = run_billing(conn, inst, close_day)
            fees = apply_late_fees(conn, inst, close_day)

            # A fee rise halfway through, so member_fees has history rather than one row.
            # It has to start from a month that is not billed yet, which the domain enforces.
            if step == args.months // 2 and people:
                raised = people[0]
                current = get_member(conn, inst, raised["id"])
                update_member(conn, inst, raised["id"], current["version"],
                              fee_change=(shift(period, 1),
                                          to_paise(str(raised["monthly_fee"] + 200))))

            statement = Statement(bank, balance)
            utr_seed = 615_000_000_000 + step * 1000
            paid_this_month = []

            # One transfer per family, not per child: a parent with two children at the
            # same centre sends one amount. Paying per child from the one UPI ID the
            # family registered would be unusual, and it is also the shape that cannot
            # be matched -- an ID belonging to several members stops rule 3 and only
            # the combined amount satisfies rule 4.
            for n, family in enumerate(families):
                here = [p for p in family if p["start_period"] <= period]
                if not here or rng.random() < 0.12:            # not joined yet, or did not pay
                    continue
                with tenant_tx(conn, inst):
                    owed = conn.execute(
                        """SELECT COALESCE(SUM(c.amount), 0)
                                  - COALESCE((SELECT SUM(a.amount) FROM allocations a
                                               JOIN charges c2 ON c2.id = a.charge_id
                                              WHERE c2.member_id = ANY(%(ids)s)), 0) AS owed
                             FROM charges c WHERE c.member_id = ANY(%(ids)s)""",
                        {"ids": [p["id"] for p in here]}).fetchone()["owed"]
                if owed <= 0:
                    continue
                paying = here if rng.random() > 0.2 else [rng.choice(here)]
                if paying != here:                             # one child's share only
                    with tenant_tx(conn, inst):
                        owed = conn.execute(
                            """SELECT COALESCE(SUM(c.amount), 0)
                                      - COALESCE((SELECT SUM(a.amount) FROM allocations a
                                                   JOIN charges c2 ON c2.id = a.charge_id
                                                  WHERE c2.member_id = %(id)s), 0) AS owed
                                 FROM charges c WHERE c.member_id = %(id)s""",
                            {"id": paying[0]["id"]}).fetchone()["owed"]
                    if owed <= 0:
                        continue
                amount = owed if rng.random() > 0.15 else owed // 2        # some pay in part
                day = date(year, month, rng.randrange(3, 27))
                utr = str(utr_seed + n)
                first = family[0]
                # How the payer identifies themselves. A family with no registered ID is
                # usually told to put the reference code in the note, and usually does.
                if first["upi_ids"]:
                    note = rng.choice(["", "", "fees", paying[0]["name"].split()[0].lower()])
                elif rng.random() > 0.25:
                    note = " ".join(p["ref_code"] for p in paying)
                else:
                    note = rng.choice(["", "fees", "monthly"])
                statement.credit(day, first["payer"], first["payer_upi"], utr, note, amount)
                paid_this_month.append((utr, paying[0]))

            payer, upi, what, amount = STRANGERS[step % len(STRANGERS)]
            statement.credit(date(year, month, 20), payer, upi, str(utr_seed + 910), what,
                             to_paise(amount))
            statement.debit(date(year, month, 7), "ATM WDL MG ROAD BANGALORE", 5000_00)
            statement.debit(date(year, month, 21), "NEFT DR RENT LANDLORD", 18000_00)
            # In one month only, the bank prints a row twice. That is a real thing and
            # the import dedupes it on the UTR -- but a re-printed row does not move the
            # running balance, so the file no longer accounts for itself and the
            # continuity check says so. Doing it every month would leave the close
            # permanently shouting, which is how a useful alert becomes noise.
            if step == 1 and len(statement.lines) > 2:
                statement.lines.insert(2, statement.lines[1])

            summary = import_statement(pool, inst, statement.text(), workers=4)
            balance = statement.balance
            totals["recorded"] += summary["recorded"]
            totals["matched"] += summary["matched"]
            totals["needs_review"] += summary["needs_review"]

            # A person works the queue: the top suggestion if it is convincing, otherwise leave it.
            for credit in review_queue(conn, inst, close_day):
                best = credit["suggestions"][0] if credit["suggestions"] else None
                if best and best["score"] >= 40 and rng.random() > 0.2:
                    assign_credit(conn, inst, credit["id"], best["member_id"])
                    totals["resolved"] += 1

            # The bank returns one transfer in the last month but one.
            if step == args.months - 2 and paid_this_month:
                with tenant_tx(conn, inst):
                    row = conn.execute("SELECT id FROM bank_credits WHERE utr = %s",
                                       (paid_this_month[-1][0],)).fetchone()
                if row:
                    reverse_credit(conn, inst, row["id"], reason="insufficient funds",
                                   reference=f"RET{utr_seed}")
                    totals["reversed"] += 1

            reminders = queue_reminders(conn, inst, close_day)
            close = run_close(conn, inst, close_day)
            print(f"  {period} [{bank:<5}] billed {billed['charges_created']:>3}, "
                  f"late fees {len(fees):>2}, imported {summary['recorded']:>3} "
                  f"({summary['matched']} auto, {summary['needs_review']} to review), "
                  f"reminders {reminders:>2}, close {close['status']}")

        with tenant_tx(conn, inst):
            owed = conn.execute(
                """SELECT COALESCE(SUM(c.amount), 0) - COALESCE((SELECT SUM(amount) FROM allocations), 0)
                     AS owed FROM charges c""").fetchone()["owed"]
            waiting = conn.execute("SELECT COUNT(*) AS n FROM bank_credits "
                                   "WHERE status = 'needs_review'").fetchone()["n"]
            decisions = conn.execute("SELECT COUNT(*) AS n FROM review_outcomes").fetchone()["n"]
        print(f"\n{totals['recorded']} credits: {totals['matched']} matched automatically, "
              f"{totals['needs_review']} went to a person, {totals['resolved']} of those resolved, "
              f"{totals['reversed']} returned by the bank")
        print(f"{waiting} still waiting, Rs {to_rupees(owed)} outstanding, "
              f"{decisions} decisions recorded")
        print("\nThe data is invented. /reports/matching and tune_weights.py will compute a rate")
        print("from it; that rate measures this generator. Real figures come from tools/pilot.py")
        print("run on one real institution's own statement.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
