"""Synthetic institutions and messy statements, for import tests and benchmarks.

Everything is driven by a seeded random.Random, so the same seed always builds
the same members and the same statement.
"""

import csv
import io
from datetime import datetime, timedelta

from paysleuth.charges import run_billing
from paysleuth.db import tenant_tx
from paysleuth.members import create_member

FEES = [150000, 180000, 200000, 250000, 300000]          # paise
FIRST = ["Diya", "Kabir", "Meera", "Arjun", "Isha", "Rohan", "Anaya", "Vivaan", "Sara", "Aditya",
         "Tara", "Reyansh", "Myra", "Advik", "Kiara", "Vihaan", "Aarohi", "Ishaan", "Riya", "Dhruv"]
SURNAMES = ["Sharma", "Verma", "Iyer", "Nair", "Reddy", "Gupta", "Rao", "Menon", "Pillai", "Das",
            "Kulkarni", "Patil", "Joshi", "Bose", "Khan", "Singh", "Mehta", "Shetty", "Naidu", "Ghosh"]
PARENTS = ["Sunita", "Rajesh", "Lakshmi", "Anil", "Priya", "Suresh", "Kavita", "Manoj", "Deepa", "Ravi"]


def build_institution(conn, inst, rng, *, pairs=20, singles=20, start="2026-06", bill_through=None):
    """Create sibling pairs that share a parent's UPI ID, plus single children.

    Returns a list of families: {"upi": str, "payer": str, "members": [(id, ref_code, fee, first)]}.
    """
    families = []
    n = 0
    for kind, count in (("pair", pairs), ("single", singles)):
        for _ in range(count):
            n += 1
            surname = rng.choice(SURNAMES)
            parent = f"{rng.choice(PARENTS)} {surname}"
            upi = f"{parent.split()[0].lower()}.{surname.lower()}{n}@oksbi"
            members = []
            for _ in range(2 if kind == "pair" else 1):
                first = rng.choice(FIRST)
                fee = rng.choice(FEES)
                view = create_member(conn, inst, name=f"{first} {surname}", guardian_name=parent,
                                     monthly_fee=fee, billing_day=rng.randint(1, 10),
                                     start_period=start, upi_ids=[upi])
                members.append((view["id"], view["ref_code"], fee, first))
            families.append({"upi": upi, "payer": parent.upper(), "members": members})
    if bill_through:
        run_billing(conn, inst, bill_through)
    return families


class _Lines:
    def __init__(self, rng, start: datetime):
        self.rng = rng
        self.start = start
        self.rows = []
        self.next_utr = 615400000000

    def utr(self) -> str:
        self.next_utr += 1
        return str(self.next_utr)

    def moment(self) -> str:
        at = self.start + timedelta(minutes=self.rng.randint(0, 29 * 24 * 60))
        return at.strftime("%d/%m/%Y %H:%M")

    def credit(self, amount: int, payer: str, upi: str, note: str = "", utr: str | None = None,
               when: str | None = None) -> list:
        utr = utr or self.utr()
        if self.rng.random() < 0.3:
            narration = f"UPI-{payer}-{upi}-SBIN0001234-{utr}-{note}".rstrip("-")
        else:
            narration = f"UPI/CR/{utr}/{payer}/SBIN/{upi}/{note}".rstrip("/")
        row = [when or self.moment(), narration, utr, "", f"{amount // 100}.{amount % 100:02d}", ""]
        self.rows.append(row)
        return row


def messy_statement(families, rng, *, rounds=1, strangers=10, start=datetime(2026, 6, 1)) -> str:
    """A shuffled statement with splits, partials, strangers, repeats, a conflict and a debit."""
    out = _Lines(rng, start)
    for _ in range(rounds):
        for family in families:
            members, upi, payer = family["members"], family["upi"], family["payer"]
            roll = rng.random()
            if len(members) == 2:
                (_, code_a, fee_a, _), (_, code_b, fee_b, first_b) = members
                if roll < 0.2:
                    out.credit(fee_a + fee_b, payer, upi, "fees")                     # together, by fees
                elif roll < 0.4:
                    out.credit(fee_a + fee_b, payer, upi, "fees")                     # together, and then
                    out.credit(fee_b, payer, upi, f"{code_b} july")                   # one child separately
                elif roll < 0.6:
                    out.credit(fee_a, payer, upi, f"{code_a} fees")                   # separately, by code
                    out.credit(fee_b, payer, upi, f"{code_b}")
                elif roll < 0.75:
                    out.credit(2 * (fee_a + fee_b), payer, upi, "june july")          # two months each
                elif roll < 0.9:
                    out.credit(fee_b, payer, upi, f"{first_b.lower()} fees")          # which child? review
                else:
                    out.credit(fee_a + fee_b, payer, upi, f"{code_a} {code_b}")       # two codes
            else:
                (_, code, fee, first) = members[0]
                if roll < 0.45:
                    out.credit(fee, payer, upi, "fees")
                elif roll < 0.65:
                    out.credit(fee // 2, payer, upi, "part")
                elif roll < 0.8:
                    out.credit(fee * 2, payer, upi, f"{code} advance")
                elif roll < 0.9:
                    out.credit(fee, "SOMEONE ELSE", f"other{rng.randint(1, 10**6)}@ybl", f"{first} fees")
    for i in range(strangers):
        out.credit(rng.choice(FEES), "UNKNOWN PAYER", f"stranger{i}@ybl", "rent")

    # The bank repeats some lines word for word, and one UTR comes back with a different amount.
    originals = list(out.rows)
    for row in rng.sample(originals, k=max(1, len(originals) // 20)):
        out.rows.append(list(row))
    victim = rng.choice(originals)
    clash = list(victim)
    clash[4] = "1.00"
    out.rows.append(clash)
    out.rows.append([out.moment(), "ATM WDL MG ROAD", "", "500.00", "", ""])

    rng.shuffle(out.rows)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Txn Date", "Narration", "Chq/Ref No", "Withdrawal Amt", "Deposit Amt", "Balance"])
    writer.writerows(out.rows)
    return buffer.getvalue()


def books(conn, inst) -> dict:
    """Everything that matters about the money, keyed by UTR and reference code, never by row id."""
    with tenant_tx(conn, inst):
        credits = conn.execute(
            "SELECT utr, amount, status, match_rule FROM bank_credits ORDER BY utr").fetchall()
        payments = conn.execute(
            """SELECT b.utr, m.ref_code, p.amount FROM payments p
                 JOIN bank_credits b ON b.id = p.bank_credit_id JOIN members m ON m.id = p.member_id
                ORDER BY b.utr, m.ref_code""").fetchall()
        allocations = conn.execute(
            """SELECT b.utr, m.ref_code, c.period, c.kind, SUM(a.amount) AS amount
                 FROM allocations a JOIN payments p ON p.id = a.payment_id
                 JOIN bank_credits b ON b.id = p.bank_credit_id JOIN members m ON m.id = p.member_id
                 JOIN charges c ON c.id = a.charge_id
                GROUP BY b.utr, m.ref_code, c.period, c.kind
               HAVING SUM(a.amount) <> 0
                ORDER BY b.utr, m.ref_code, c.period, c.kind""").fetchall()
        receipts = conn.execute(
            """SELECT m.ref_code, o.payload - 'message_id' - 'member_id' AS payload
                 FROM outbox o JOIN members m ON m.id = o.member_id
                WHERE o.kind = 'payment_receipt' ORDER BY m.ref_code, o.id""").fetchall()
    return {"credits": credits, "payments": payments, "allocations": allocations, "receipts": receipts}
