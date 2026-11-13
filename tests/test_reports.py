"""The reports must agree with each other and with the balances they are built from.

Each report computes its own sums in SQL, from charges and allocations, with no
stored balance to lean on. That is the design, but it means four independent
queries could drift apart. These tests build one messy institution (mixed fees,
concessions, a full scholarship, partial payments, late fees, a reversal) and
then check the reports against each other and against allocation.balances().
"""

import random
from datetime import date, datetime

import pytest

from conftest import add_member, credit
from paysleuth.allocation import balances
from paysleuth.charges import apply_late_fees, run_billing
from paysleuth.db import tenant_tx
from paysleuth.institutions import create_institution
from paysleuth.money import to_paise, to_rupees
from paysleuth.payments import record_credit, reverse_credit
from paysleuth.periods import IST
from paysleuth.reports import ageing, collection_report, defaulters, statement

AS_OF = date(2026, 6, 20)
BUCKETS = ("not_yet_due", "days_1_30", "days_31_60", "days_61_90", "over_90")


def paise(rupees: str) -> int:
    """Parse a report's rupee string back to paise, zero and negatives included.

    money.to_paise refuses zero and negatives on purpose, since no amount a
    payer sends is zero; a report total legitimately can be.
    """
    sign = -1 if rupees.startswith("-") else 1
    whole, rest = rupees.lstrip("-").split(".")
    return sign * (int(whole) * 100 + int(rest))


@pytest.fixture
def centre(conn, inst):
    """Twelve members billed from March, some paid, one credit returned by the bank."""
    rng = random.Random(3)
    ids = [add_member(conn, inst, f"Child {i}", fee=str(1000 + 250 * i), start="2026-03",
                      concession=rng.choice([0, 0, 25, 100])) for i in range(12)]
    run_billing(conn, inst, AS_OF)
    apply_late_fees(conn, inst, AS_OF)
    for n, member_id in enumerate(ids):
        if rng.random() < 0.7:
            record_credit(conn, inst, credit(utr=f"7000000000{n:02d}",
                                             amount=str(rng.choice([500, 1500, 4000])),
                                             at=datetime(2026, 6, 10, tzinfo=IST),
                                             note=f"PS{1000 + member_id}"))
    with tenant_tx(conn, inst):
        returned = conn.execute("SELECT id FROM bank_credits ORDER BY id LIMIT 1").fetchone()
    if returned:
        reverse_credit(conn, inst, returned["id"])
    return ids


def test_the_ageing_buckets_add_up_three_ways(conn, inst, centre):
    report = ageing(conn, inst, AS_OF)
    for row in report["members"]:
        assert sum(paise(row[b]) for b in BUCKETS) == paise(row["total"]), row["ref_code"]
    for bucket in BUCKETS:
        assert sum(paise(r[bucket]) for r in report["members"]) == paise(report["totals"][bucket]), bucket
    assert sum(paise(r["total"]) for r in report["members"]) == paise(report["totals"]["total"])
    assert report["as_of"] == AS_OF.isoformat()


def test_the_ageing_total_is_what_every_member_owes(conn, inst, centre):
    with tenant_tx(conn, inst):
        owed = sum(b["outstanding"] for b in balances(conn, centre).values())
    assert ageing(conn, inst, AS_OF)["totals"]["total"] == to_rupees(owed)
    # Members who owe nothing are left out rather than listed as zero rows.
    listed = {r["member_id"] for r in ageing(conn, inst, AS_OF)["members"]}
    with tenant_tx(conn, inst):
        money = balances(conn, centre)
    assert listed == {m for m in centre if money[m]["outstanding"] > 0}


@pytest.mark.parametrize("period", ["2026-03", "2026-04", "2026-05", "2026-06"])
def test_each_months_collection_report_reconciles(conn, inst, centre, period):
    report = collection_report(conn, inst, period)
    assert paise(report["billed"]) - paise(report["collected"]) == paise(report["outstanding"])
    assert report["paid_in_full"] + len(report["owing"]) == report["members_billed"]
    assert all(paise(owing["outstanding"]) > 0 for owing in report["owing"])


def test_a_month_nobody_was_billed_for_reports_zero_not_a_division_error(conn, inst, centre):
    report = collection_report(conn, inst, "2027-01")
    assert report["members_billed"] == 0 and report["billed"] == "0.00"
    assert report["collection_rate_pct"] == "0.0" and report["owing"] == []


def test_each_member_statement_agrees_with_the_balances_it_came_from(conn, inst, centre):
    for member_id in centre:
        lines = statement(conn, inst, member_id, AS_OF)
        with tenant_tx(conn, inst):
            money = balances(conn, [member_id])[member_id]
        assert lines["totals"]["outstanding"] == to_rupees(money["outstanding"])
        assert lines["totals"]["credit"] == to_rupees(money["credit"])
        assert sum(paise(c["amount"]) for c in lines["charges"]) == paise(lines["totals"]["billed"])
        # Nothing is shown as paid beyond what it cost, and the statuses follow the numbers.
        for charge in lines["charges"]:
            assert paise(charge["paid"]) <= paise(charge["amount"])
            assert paise(charge["gross"]) - paise(charge["concession"]) == paise(charge["amount"])
            expected = ("paid" if charge["paid"] == charge["amount"]
                        else "partially_paid" if paise(charge["paid"]) > 0 else "unpaid")
            assert charge["status"] == expected
            # Overdue only if something is still owed and the due date has passed:
            # a fully paid charge, and one not yet due, are both nought days overdue.
            overdue_days = max(0, (AS_OF - date.fromisoformat(charge["due_date"])).days)
            assert charge["days_overdue"] == (overdue_days if expected != "paid" else 0)


def test_a_reversed_payment_leaves_the_statement_and_the_reports_consistent(conn, inst, centre):
    """A reversal writes a negative payment row. Every report must net it out the same way."""
    with tenant_tx(conn, inst):
        reversed_member = conn.execute(
            "SELECT member_id FROM payments WHERE reverses_id IS NOT NULL LIMIT 1").fetchone()
    assert reversed_member is not None, "the fixture should have reversed one credit"
    member_id = reversed_member["member_id"]
    lines = statement(conn, inst, member_id, AS_OF)
    # The voided payment is gone from the statement's payment list, not shown as negative.
    assert all(paise(p["amount"]) > 0 for p in lines["payments"])
    with tenant_tx(conn, inst):
        money = balances(conn, [member_id])[member_id]
    assert lines["totals"]["paid"] == to_rupees(money["paid"])
    assert lines["totals"]["outstanding"] == to_rupees(money["outstanding"])


def test_defaulters_is_a_subset_of_the_ageing_report(conn, inst, centre):
    overdue = defaulters(conn, inst, AS_OF, min_days=30)
    ages = {r["member_id"]: r for r in ageing(conn, inst, AS_OF)["members"]}
    assert [d["member_id"] for d in overdue] == sorted(
        [d["member_id"] for d in overdue],
        key=lambda m: (-paise(next(x["overdue"] for x in overdue if x["member_id"] == m)), m))
    for entry in overdue:
        assert entry["member_id"] in ages
        assert paise(entry["overdue"]) > 0 and entry["oldest_days_overdue"] >= 30
        # What is overdue by 30 days or more cannot exceed what is owed in total.
        assert paise(entry["overdue"]) <= paise(ages[entry["member_id"]]["total"])
    assert defaulters(conn, inst, AS_OF, min_days=10_000) == []


def test_the_reports_are_empty_and_not_broken_for_a_new_institution(conn, inst):
    assert ageing(conn, inst, AS_OF) == {
        "as_of": AS_OF.isoformat(), "members": [],
        "totals": {b: "0.00" for b in (*BUCKETS, "total")}}
    assert defaulters(conn, inst, AS_OF) == []
    report = collection_report(conn, inst, "2026-06")
    assert report["collection_rate_pct"] == "0.0" and report["credits_waiting_for_review"] == 0


def test_a_member_with_one_charge_and_no_payment(conn, inst):
    """One row is where an aggregate is most likely to be written wrong."""
    solo = add_member(conn, inst, "Solo", fee="1234.56", start="2026-06")
    run_billing(conn, inst, date(2026, 6, 1))
    lines = statement(conn, inst, solo, AS_OF)
    assert lines["totals"] == {"billed": "1234.56", "paid": "0.00", "outstanding": "1234.56",
                               "credit": "0.00"}
    assert lines["charges"][0]["days_overdue"] == 15 and lines["payments"] == []
    ages = ageing(conn, inst, AS_OF)
    assert ages["totals"]["days_1_30"] == "1234.56" and ages["totals"]["total"] == "1234.56"
    assert collection_report(conn, inst, "2026-06")["collection_rate_pct"] == "0.0"


def test_a_concession_that_rounds_to_a_half_paisa_still_reconciles(conn, inst):
    odd = add_member(conn, inst, "Odd", fee="33.33", start="2026-06", concession=50)
    run_billing(conn, inst, date(2026, 6, 1))
    record_credit(conn, inst, credit(amount="16.67", note=f"PS{1000 + odd}"))
    lines = statement(conn, inst, odd, AS_OF)
    charge = lines["charges"][0]
    assert (charge["gross"], charge["concession"], charge["amount"]) == ("33.33", "16.67", "16.66")
    assert charge["status"] == "paid" and lines["totals"]["credit"] == "0.01"
    assert ageing(conn, inst, AS_OF)["totals"]["total"] == "0.00"


def test_the_reports_never_see_another_institutions_money(conn, inst, centre):
    other, _ = create_institution(conn, "Other Centre")
    stranger = add_member(conn, other, "Stranger", fee="9999", start="2026-03")
    run_billing(conn, other, AS_OF)
    assert ageing(conn, other, AS_OF)["totals"]["total"] == to_rupees(4 * to_paise("9999"))
    assert [r["member_id"] for r in ageing(conn, other, AS_OF)["members"]] == [stranger]
    assert collection_report(conn, other, "2026-06")["members_billed"] == 1
    # And the first institution's reports are unchanged by the second existing.
    assert [r["member_id"] for r in ageing(conn, inst, AS_OF)["members"]] != [stranger]
