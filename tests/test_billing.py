from datetime import date, datetime, timezone

import pytest

from conftest import add_member, charges_of, credit, money
from paysleuth.allocation import settle_members
from paysleuth.charges import apply_late_fees, bill_member, concession_amount, run_billing
from paysleuth.db import lock_members, tenant_tx
from paysleuth.errors import Conflict, PreconditionFailed
from paysleuth.members import (deactivate_member, get_member, normalise_phone, normalise_upi_id,
                               update_member)
from paysleuth.payments import record_credit
from paysleuth.periods import IST


def _periods(conn, inst, member_id):
    return [(c["period"], c["kind"], c["amount"]) for c in charges_of(conn, inst, member_id)]


def test_each_month_is_billed_once_and_missed_months_catch_up(conn, inst):
    diya = add_member(conn, inst, start="2026-06")
    run_billing(conn, inst, date(2026, 6, 1))
    run_billing(conn, inst, date(2026, 6, 20))           # same month again: nothing new
    assert _periods(conn, inst, diya) == [("2026-06", "monthly_fee", 200000)]

    summary = run_billing(conn, inst, date(2026, 9, 2))  # July and August were missed
    assert summary["charges_created"] == 3
    assert [p for p, _, _ in _periods(conn, inst, diya)] == ["2026-06", "2026-07", "2026-08", "2026-09"]


def test_a_member_who_starts_later_is_not_billed_early(conn, inst):
    kabir = add_member(conn, inst, "Kabir", start="2026-08")
    run_billing(conn, inst, date(2026, 6, 10))
    assert _periods(conn, inst, kabir) == []


def test_billing_day_31_in_february_falls_on_the_last_day(conn, inst):
    meera = add_member(conn, inst, "Meera", billing_day=31, start="2027-02")
    run_billing(conn, inst, date(2027, 2, 1))
    with tenant_tx(conn, inst):
        due = conn.execute("SELECT due_date FROM charges WHERE member_id = %s", (meera,)).fetchone()
    assert due["due_date"] == date(2027, 2, 28)


def test_fee_change_for_a_billed_month_is_refused_and_a_future_month_applies(conn, inst):
    diya = add_member(conn, inst, start="2026-06")
    run_billing(conn, inst, date(2026, 6, 1))
    version = get_member(conn, inst, diya)["version"]
    with pytest.raises(Conflict):
        update_member(conn, inst, diya, version, fee_change=("2026-06", 250000))
    # The refused edit rolled back entirely, including the version bump.
    assert get_member(conn, inst, diya)["version"] == version

    view = update_member(conn, inst, diya, version, fee_change=("2026-08", 250000))
    assert [f["monthly_fee"] for f in view["fee_history"]] == ["2000.00", "2500.00"]
    run_billing(conn, inst, date(2026, 8, 1))
    assert [a for _, _, a in _periods(conn, inst, diya)] == [200000, 200000, 250000]


def test_a_scholarship_month_is_not_billed_later_when_the_scholarship_ends(conn, inst):
    asha = add_member(conn, inst, "Asha", start="2026-06", concession=100)
    run_billing(conn, inst, date(2026, 7, 1))
    assert _periods(conn, inst, asha) == []
    version = get_member(conn, inst, asha)["version"]
    update_member(conn, inst, asha, version, changes={"concession_pct": 0})
    run_billing(conn, inst, date(2026, 8, 1))
    assert _periods(conn, inst, asha) == [("2026-08", "monthly_fee", 200000)]


def test_concessions_round_to_the_nearest_paisa():
    assert concession_amount(200000, 25) == 50000
    assert concession_amount(333, 50) == 167            # 166.5 rounds half up
    assert concession_amount(1, 49) == 0
    assert concession_amount(200000, 100) == 200000


def test_concession_is_stored_on_the_charge(conn, inst):
    rohan = add_member(conn, inst, "Rohan", fee="1999.99", start="2026-06", concession=15)
    run_billing(conn, inst, date(2026, 6, 1))
    with tenant_tx(conn, inst):
        c = conn.execute("SELECT gross, concession, amount FROM charges WHERE member_id = %s",
                         (rohan,)).fetchone()
    assert c == {"gross": 199999, "concession": 30000, "amount": 169999}


def test_late_fee_only_after_the_grace_period_and_only_once_a_month(conn, inst):
    diya = add_member(conn, inst, start="2026-06", billing_day=5)   # due June 5, grace 3 days
    run_billing(conn, inst, date(2026, 6, 1))
    assert apply_late_fees(conn, inst, date(2026, 6, 8)) == []      # still inside the grace period
    assert len(apply_late_fees(conn, inst, date(2026, 6, 9))) == 1
    assert apply_late_fees(conn, inst, date(2026, 6, 20)) == []     # already charged for June
    assert _periods(conn, inst, diya) == [("2026-06", "monthly_fee", 200000),
                                          ("2026-06", "late_fee", 10000)]


def test_no_late_fee_for_a_deactivated_member(conn, inst):
    diya = add_member(conn, inst, start="2026-06")
    run_billing(conn, inst, date(2026, 6, 1))
    deactivate_member(conn, inst, diya, get_member(conn, inst, diya)["version"])
    assert apply_late_fees(conn, inst, date(2026, 6, 30)) == []
    run_billing(conn, inst, date(2026, 7, 1))
    assert len(_periods(conn, inst, diya)) == 1


def test_optimistic_locking_keeps_the_first_save(conn, inst):
    diya = add_member(conn, inst, start="2026-06")
    update_member(conn, inst, diya, 1, changes={"guardian_name": "Sunita Sharma"})
    with pytest.raises(PreconditionFailed):
        update_member(conn, inst, diya, 1, changes={"guardian_name": "Someone Else"})
    view = get_member(conn, inst, diya)
    assert view["guardian_name"] == "Sunita Sharma" and view["version"] == 2


def test_only_known_fields_can_be_edited(conn, inst):
    diya = add_member(conn, inst)
    with pytest.raises(ValueError):
        update_member(conn, inst, diya, 1, changes={"billed_through": "2030-01"})
    with pytest.raises(ValueError):
        update_member(conn, inst, diya, 1, changes={"billing_day": 32})


def _raw_payment(conn, inst, member_id, amount, utr="UTR000000001"):
    with tenant_tx(conn, inst):
        credit = conn.execute(
            """INSERT INTO bank_credits (institution_id, utr, amount, received_at, status, match_rule)
               VALUES (%s, %s, %s, %s, 'matched', 'manual') RETURNING id""",
            (inst, utr, amount, datetime(2026, 6, 10, tzinfo=timezone.utc))).fetchone()["id"]
        conn.execute("INSERT INTO payments (institution_id, bank_credit_id, member_id, amount, received_at) "
                     "VALUES (%s, %s, %s, %s, %s)",
                     (inst, credit, member_id, amount, datetime(2026, 6, 10, tzinfo=timezone.utc)))
        lock_members(conn, [member_id])
        return settle_members(conn, inst, [member_id])


def test_settle_pays_the_oldest_charge_first(conn, inst):
    diya = add_member(conn, inst, start="2026-06")
    run_billing(conn, inst, date(2026, 7, 1))                  # June and July, Rs 2,000 each
    created = _raw_payment(conn, inst, diya, 300000)
    assert [(c["period"], c["amount"]) for c in created] == [("2026-06", 200000), ("2026-07", 100000)]
    assert [c["paid"] for c in charges_of(conn, inst, diya)] == [200000, 100000]
    assert money(conn, inst, diya) == {"billed": 400000, "paid": 300000, "outstanding": 100000, "credit": 0}


def test_an_advance_pays_new_charges_as_soon_as_they_are_billed(conn, inst):
    diya = add_member(conn, inst, start="2026-06")
    run_billing(conn, inst, date(2026, 6, 1))
    _raw_payment(conn, inst, diya, 500000)                      # Rs 5,000 against Rs 2,000
    assert money(conn, inst, diya)["credit"] == 300000
    bill_member(conn, inst, diya, "2026-07")
    assert money(conn, inst, diya) == {"billed": 400000, "paid": 500000, "outstanding": 0, "credit": 100000}


def test_upi_ids_and_phones_are_normalised():
    assert normalise_upi_id(" Sunita.Sharma@OKSBI ") == "sunita.sharma@oksbi"
    assert normalise_upi_id("") is None
    for bad in ["no-at-sign", "a@", "@okaxis", "x y@ok"]:
        with pytest.raises(ValueError):
            normalise_upi_id(bad)
    assert normalise_phone("+91 98765 43210") == "9876543210"
    assert normalise_phone("09876543210") == "9876543210"
    for bad in ["12345", "5876543210", "+91 98765"]:
        with pytest.raises(ValueError):
            normalise_phone(bad)


def test_reference_codes_follow_the_member_id(conn, inst):
    diya = add_member(conn, inst)
    assert get_member(conn, inst, diya)["ref_code"] == f"PS{1000 + diya}"


# --- billing with real payments ------------------------------------------------------------

def test_no_late_fee_once_the_fee_is_paid(conn, inst):
    diya = add_member(conn, inst, start="2026-06")
    run_billing(conn, inst, date(2026, 6, 1))
    record_credit(conn, inst, credit(note=f"PS{1000 + diya}", at=datetime(2026, 6, 7, tzinfo=IST)))
    assert apply_late_fees(conn, inst, date(2026, 6, 20)) == []


def test_a_part_paid_month_gets_a_late_fee_and_both_can_be_cleared(conn, inst):
    diya = add_member(conn, inst, start="2026-06")
    run_billing(conn, inst, date(2026, 6, 1))
    record_credit(conn, inst, credit(utr="615412345601", amount="1500", note=f"PS{1000 + diya}"))
    apply_late_fees(conn, inst, date(2026, 6, 20))
    assert money(conn, inst, diya)["outstanding"] == 50000 + 10000
    record_credit(conn, inst, credit(utr="615412345602", amount="600", note=f"PS{1000 + diya}",
                                     at=datetime(2026, 6, 21, tzinfo=IST)))
    assert [c["paid"] for c in charges_of(conn, inst, diya)] == [200000, 10000]
