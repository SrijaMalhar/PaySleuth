"""A narrated run of the parts that are hard to show in a README.

    python -m tools.walkthrough

Five beats, each doing real work against a real database: billing, an import
that decides what it can and refuses what it cannot, the queue a person works,
one credit delivered twenty times at once, and a statement line that never
became a row. The last one is the point of the project, and nothing but a
running system demonstrates it.

Writes into its own schema and drops it afterwards, so it can be run against a
database that already has data in it.
"""

import argparse
import os
import sys
import threading
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paysleuth.charges import apply_late_fees, run_billing       # noqa: E402
from paysleuth.db import make_pool, migrate, tenant_tx            # noqa: E402
from paysleuth.importer import import_statement                   # noqa: E402
from paysleuth.institutions import create_institution             # noqa: E402
from paysleuth.members import create_member                       # noqa: E402
from paysleuth.money import to_paise, to_rupees                   # noqa: E402
from paysleuth.payments import record_credit, review_queue        # noqa: E402
from paysleuth.statements import Credit                         # noqa: E402

AS_OF = date(2026, 6, 20)
PERIOD = "2026-06"

STUDENTS = [
    ("Diya Sharma", "Sunita Sharma", "2000", 0, ["sunita.sharma@oksbi"]),
    ("Rohan Verma", "Rajesh Kumar Verma", "2000", 0, ["rk.verma@okhdfcbank"]),
    ("Kabir Verma", "Rajesh Kumar Verma", "1500", 0, ["rk.verma@okhdfcbank"]),
    ("Arjun Nair", "Anil Nair", "1800", 0, []),
    ("Meera Iyer", "Lakshmi Iyer", "2500", 25, []),
]

# A real HDFC export's columns, and the narration shapes real banks print.
STATEMENT = """Txn Date,Narration,Chq/Ref No,Withdrawal Amt,Deposit Amt,Closing Balance
05/06/2026,UPI/CR/615412340001/SUNITA SHARMA/SBIN/sunita.sharma@oksbi/,615412340001,,"2,000.00","42,000.00"
06/06/2026,UPI-R K VERMA-rk.verma@okhdfcbank-HDFC0001234-615412340002-JUNE FEES,615412340002,,"3,500.00","45,500.00"
07/06/2026,ATM WDL MG ROAD BANGALORE,,"5,000.00",,"40,500.00"
08/06/2026,UPI/CR/615412340004/A NAIR/SBIN/anil.nair@oksbi/{code},615412340004,,"1,800.00","42,300.00"
09/06/2026,UPI/CR/615412340005/LAKSHMI IYER/UBIN/lakshmi.iyer@oksbi/meera fees,615412340005,,"1,875.00","44,175.00"
10/06/2026,UPI/CR/615412340006/GREEN LEAF CAFE/SBIN/greenleaf@oksbi/rent june,615412340006,,"12,000.00","56,175.00"
"""

# July, and the second line is one no parser can read: a narration with no UPI
# fields and no reference number to fall back on. The amount is legible, so the
# bank definitely credited Rs 12,000 -- but nothing will become a row for it.
BROKEN = """Txn Date,Narration,Chq/Ref No,Withdrawal Amt,Deposit Amt,Closing Balance
05/07/2026,UPI/CR/615412350001/SUNITA SHARMA/SBIN/sunita.sharma@oksbi/,615412350001,,"2,000.00","58,175.00"
06/07/2026,NEFT INWARD RETURN REF 99 UNREADABLE,,,"12,000.00","70,175.00"
"""

BOLD, DIM, GREEN, RED, YELLOW, CYAN, OFF = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m")


def beat(n, title):
    print(f"\n{BOLD}{CYAN}[{n}/5] {title}{OFF}")


def line(text=""):
    print(f"      {text}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.environ.get(
        "PAYSLEUTH_DATABASE_URL", "postgresql://paysleuth:paysleuth@localhost:5432/paysleuth"))
    args = parser.parse_args(argv)

    schema = f"walkthrough_{os.getpid()}"
    pool = make_pool(args.database_url, max_size=8, options=f"-c search_path={schema}")
    with pool.connection() as c:
        c.execute(f"CREATE SCHEMA {schema}")
    try:
        return run(pool)
    finally:
        with make_pool(args.database_url, max_size=1) as clean, clean.connection() as c:
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def run(pool) -> int:
    migrate(pool)
    with pool.connection() as conn:
        inst, _ = create_institution(conn, "Bright Minds Tuition", late_fee=to_paise("100"),
                                     grace_days=5, owner_name="Asha")
        codes = {}
        for name, guardian, fee, concession, upi_ids in STUDENTS:
            view = create_member(conn, inst, name=name, guardian_name=guardian,
                                 monthly_fee=to_paise(fee), billing_day=5, start_period=PERIOD,
                                 upi_ids=upi_ids, concession_pct=concession)
            codes[name.split()[0]] = view["ref_code"]

        beat(1, "Bill the month")
        billed = run_billing(conn, inst, AS_OF)
        fees = apply_late_fees(conn, inst, AS_OF)
        line(f"{billed['charges_created']} charges for {billed['members']} students, "
             f"{len(fees)} late fees after the 5-day grace")
        line(f"{DIM}no balance column anywhere: what is owed is a sum over charges "
             f"minus allocations{OFF}")

        beat(2, "Import the bank's own CSV")
        text = STATEMENT.format(code=codes["Arjun"])
        summary = import_statement(pool, inst, text, workers=4)
        line(f"6 lines: {summary['recorded']} credits recorded, "
             f"{summary['skipped_debits']} debit skipped")
        line(f"{GREEN}{summary['matched']} matched with nobody involved{OFF}, "
             f"{YELLOW}{summary['needs_review']} sent to a person{OFF}")
        with tenant_tx(conn, inst):
            rows = conn.execute(
                """SELECT b.amount, b.match_rule AS rule, b.status, string_agg(m.name, ' + ' ORDER BY m.id) AS who
                     FROM bank_credits b
                     LEFT JOIN payments p ON p.bank_credit_id = b.id
                     LEFT JOIN members m ON m.id = p.member_id
                    GROUP BY b.id, b.amount, b.match_rule, b.status ORDER BY b.status, b.amount""").fetchall()
        for r in rows:
            if r["status"] == "matched":
                line(f"  {GREEN}OK{OFF}  Rs {to_rupees(r['amount']):>9}  {r['who']:<28} {DIM}{r['rule']}{OFF}")
            else:
                line(f"  {YELLOW}??{OFF}  Rs {to_rupees(r['amount']):>9}  {'a person must decide':<28}")

        beat(3, "What a person actually sees")
        for credit in review_queue(conn, inst, AS_OF):
            line(f"Rs {credit['amount']} from {credit['payer_name']}"
                 f"{(': ' + credit['note']) if credit.get('note') else ''}")
            for s in credit["suggestions"][:3]:
                line(f"  {s['score']:>4}  {s['ref_code']} {s['name']}")
                for reason in s["reasons"]:
                    line(f"        {DIM}- {reason}{OFF}")
        line(f"{DIM}ranked, explained, and never applied -- a wrong attribution is worse "
             f"than none{OFF}")

        beat(4, "The same credit, delivered 20 times at once")
        credit = Credit(utr="615412349999", amount=to_paise("2000"),
                        received_at=datetime(2026, 6, 11, 9, 0, tzinfo=UTC),
                        payer_name="SUNITA SHARMA", payer_upi_id="sunita.sharma@oksbi", note="")
        start = threading.Barrier(20)

        def deliver():
            start.wait()
            with pool.connection() as c:
                record_credit(c, inst, credit)

        threads = [threading.Thread(target=deliver) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with tenant_tx(conn, inst):
            n = conn.execute("SELECT COUNT(*) AS n FROM bank_credits WHERE utr = %s",
                             ("615412349999",)).fetchone()["n"]
            paid = conn.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM payments "
                                "WHERE bank_credit_id = (SELECT id FROM bank_credits WHERE utr = %s)",
                                ("615412349999",)).fetchone()["s"]
        line(f"rows for that UTR: {GREEN}{n}{OFF}   applied to Diya: {GREEN}Rs {to_rupees(paid)}{OFF}")
        line(f"{DIM}UNIQUE (institution_id, utr) -- the key belongs to the bank, not to us{OFF}")

        beat(5, "A statement line that never became a row")
        july = import_statement(pool, inst, BROKEN, workers=2)
        line(f"July statement: 2 credit lines printed, "
             f"{GREEN}{july['recorded']} recorded{OFF}, "
             f"{RED}Rs {july['statement']['credits_dropped']} dropped{OFF}")
        line(f"{DIM}the import reports success. Summing what landed would balance{OFF}")
        line(f"{DIM}perfectly, because the lost line is absent from both sides.{OFF}")
        with tenant_tx(conn, inst):
            alert = conn.execute(
                "SELECT kind, details FROM alerts WHERE status = 'open' AND kind = 'credits_missing' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        if alert:
            for missing in alert["details"]["missing"]:
                line(f"{RED}ALERT {alert['kind']}{OFF}: line {missing['line']} printed "
                     f"Rs {missing['amount']}; the books have no row for it")
        line(f"{DIM}caught by asking the books for every (UTR, amount) the statement{OFF}")
        line(f"{DIM}printed, instead of trusting the importer's report of its own work{OFF}")

        print(f"\n{BOLD}  Every number above came from a real PostgreSQL. Schema dropped on exit.{OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
