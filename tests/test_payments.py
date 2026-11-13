from dataclasses import replace
from datetime import date, datetime

import pytest

from conftest import add_member, charges_of, credit, money, receipts
from paysleuth.charges import run_billing
from paysleuth.db import tenant_tx
from paysleuth.errors import Conflict, NotFound
from paysleuth.institutions import create_institution
from paysleuth.members import get_member
from paysleuth.payments import assign_credit, reassign_credit, record_credit, review_queue, split_credit
from paysleuth.periods import IST

JUNE = date(2026, 6, 1)


def code(member_id):
    return f"PS{1000 + member_id}"


def _status(result):
    return result["credit"]["status"], result["credit"]["match_rule"]


# --- matching ----------------------------------------------------------------------------

def test_a_reference_code_pays_that_member(conn, inst):
    diya = add_member(conn, inst)
    run_billing(conn, inst, JUNE)
    result = record_credit(conn, inst, credit(note=f"june fees {code(diya)}"))
    assert _status(result) == ("matched", "ref_code")
    assert result["applied"][0]["covered"] == [{"period": "2026-06", "kind": "monthly_fee", "amount": "2000.00"}]
    assert money(conn, inst, diya)["outstanding"] == 0


def test_a_registered_upi_id_pays_that_member(conn, inst):
    diya = add_member(conn, inst, upi_ids=["sunita.sharma@oksbi"])
    run_billing(conn, inst, JUNE)
    result = record_credit(conn, inst, credit(upi="Sunita.Sharma@OKSBI"))
    assert _status(result) == ("matched", "known_upi_id")
    assert money(conn, inst, diya)["outstanding"] == 0


def test_one_transfer_for_two_siblings_splits_by_what_each_owes(conn, inst):
    diya = add_member(conn, inst, fee="2000", upi_ids=["rk.verma@okhdfcbank"])
    kabir = add_member(conn, inst, "Kabir", fee="1500", upi_ids=["rk.verma@okhdfcbank"])
    run_billing(conn, inst, JUNE)
    result = record_credit(conn, inst, credit(amount="3500", upi="rk.verma@okhdfcbank"))
    assert _status(result) == ("matched", "sibling_split")
    assert [(a["member_id"], a["amount"]) for a in result["applied"]] == [(diya, "2000.00"), (kabir, "1500.00")]
    assert money(conn, inst, diya)["outstanding"] == money(conn, inst, kabir)["outstanding"] == 0


def test_two_codes_split_when_the_amount_is_exact(conn, inst):
    diya = add_member(conn, inst, fee="2000")
    kabir = add_member(conn, inst, "Kabir", fee="1500")
    run_billing(conn, inst, JUNE)
    result = record_credit(conn, inst, credit(amount="3500", note=f"{code(diya)} {code(kabir)}"))
    assert _status(result) == ("matched", "sibling_split")


def test_siblings_paying_in_advance_split_by_their_fees(conn, inst):
    diya = add_member(conn, inst, fee="2000", upi_ids=["sunita@oksbi"])
    kabir = add_member(conn, inst, "Kabir", fee="1500", upi_ids=["sunita@oksbi"])
    record_credit(conn, inst, credit(amount="3500", upi="sunita@oksbi"))   # nothing billed yet
    assert money(conn, inst, diya)["credit"] == 200000 and money(conn, inst, kabir)["credit"] == 150000
    run_billing(conn, inst, JUNE)
    assert money(conn, inst, diya)["outstanding"] == money(conn, inst, kabir)["outstanding"] == 0


def test_an_ambiguous_amount_goes_to_review(conn, inst):
    add_member(conn, inst, fee="2000", upi_ids=["sunita@oksbi"])
    add_member(conn, inst, "Kabir", fee="1500", upi_ids=["sunita@oksbi"])
    run_billing(conn, inst, JUNE)
    result = record_credit(conn, inst, credit(amount="2000", upi="sunita@oksbi"))
    assert _status(result) == ("needs_review", None) and result["applied"] == []


def test_an_unknown_payer_goes_to_review(conn, inst):
    add_member(conn, inst)
    result = record_credit(conn, inst, credit(upi="lakshmi.iyer@oksbi", note="meera fees"))
    assert _status(result) == ("needs_review", None)


def test_an_unknown_code_falls_back_to_the_upi_id(conn, inst):
    diya = add_member(conn, inst, upi_ids=["sunita@oksbi"])
    result = record_credit(conn, inst, credit(upi="sunita@oksbi", note="PS99999 fees"))
    assert _status(result) == ("matched", "known_upi_id")
    assert result["applied"][0]["member_id"] == diya


# --- idempotency -------------------------------------------------------------------------

def test_the_same_utr_counts_once(conn, inst):
    diya = add_member(conn, inst)
    run_billing(conn, inst, JUNE)
    first = record_credit(conn, inst, credit(note=code(diya)))
    again = record_credit(conn, inst, credit(utr=" 6154 1234 5601 ", note=code(diya)))
    assert first["duplicate"] is False and again["duplicate"] is True
    assert again["credit"]["id"] == first["credit"]["id"] and again["applied"] == []
    assert money(conn, inst, diya)["paid"] == 200000
    assert len(receipts(conn, inst, diya)) == 1


def test_the_same_utr_with_a_different_amount_is_a_conflict(conn, inst):
    record_credit(conn, inst, credit(amount="2000"))
    with pytest.raises(Conflict):
        record_credit(conn, inst, credit(amount="2500"))


def test_bad_utrs_and_amounts_are_refused(conn, inst):
    for bad in ["", "12345", "UTR-WITH-DASH", "X" * 36]:
        with pytest.raises(ValueError):
            record_credit(conn, inst, credit(utr=bad))
    with pytest.raises(ValueError):
        record_credit(conn, inst, replace(credit(), amount=0))


def test_the_same_utr_in_another_institution_is_a_different_credit(conn, inst):
    other, _ = create_institution(conn, "Other Centre")
    assert record_credit(conn, inst, credit())["duplicate"] is False
    assert record_credit(conn, other, credit())["duplicate"] is False


# --- applying money ----------------------------------------------------------------------

def test_partial_payment_then_the_rest(conn, inst):
    diya = add_member(conn, inst)
    run_billing(conn, inst, JUNE)
    record_credit(conn, inst, credit(utr="615412345601", amount="1200", note=code(diya)))
    assert money(conn, inst, diya)["outstanding"] == 80000
    record_credit(conn, inst, credit(utr="615412345602", amount="800", note=code(diya)))
    assert money(conn, inst, diya) == {"billed": 200000, "paid": 200000, "outstanding": 0, "credit": 0}


def test_overpayment_becomes_credit_and_pays_next_month(conn, inst):
    diya = add_member(conn, inst)
    run_billing(conn, inst, JUNE)
    result = record_credit(conn, inst, credit(amount="5000", note=code(diya)))
    assert result["applied"][0]["credit_after"] == "3000.00"
    run_billing(conn, inst, date(2026, 7, 1))
    assert money(conn, inst, diya)["credit"] == 100000
    assert [c["paid"] for c in charges_of(conn, inst, diya)] == [200000, 200000]


def test_old_dues_are_paid_first(conn, inst):
    diya = add_member(conn, inst, start="2026-04")
    run_billing(conn, inst, JUNE)                                 # April, May, June
    record_credit(conn, inst, credit(amount="3000", note=code(diya)))
    assert [c["paid"] for c in charges_of(conn, inst, diya)] == [200000, 100000, 0]


def test_every_applied_payment_queues_one_receipt(conn, inst):
    diya = add_member(conn, inst, fee="2000", upi_ids=["sunita@oksbi"])
    kabir = add_member(conn, inst, "Kabir", fee="1500", upi_ids=["sunita@oksbi"])
    run_billing(conn, inst, JUNE)
    record_credit(conn, inst, credit(amount="3500", upi="sunita@oksbi"))
    record_credit(conn, inst, credit(utr="615412345699", amount="999", upi="stranger@ybl"))  # review: no receipt
    sent = receipts(conn, inst)
    assert [(r["member_id"], r["amount"]) for r in sent] == [(diya, "2000.00"), (kabir, "1500.00")]
    assert sent[0]["utr"] == "615412345601" and sent[0]["outstanding_after"] == "0.00"
    assert len({r["message_id"] for r in sent}) == 2


# --- manual actions ----------------------------------------------------------------------

def _queued(conn, inst, utr="615412345601", amount="2000", **kw):
    return record_credit(conn, inst, credit(utr=utr, amount=amount, upi=kw.pop("upi", "lakshmi@oksbi"),
                                            **kw))["credit"]["id"]


def test_assign_pays_and_learns_the_upi_id(conn, inst):
    meera = add_member(conn, inst, "Meera")
    run_billing(conn, inst, JUNE)
    credit_id = _queued(conn, inst)
    result = assign_credit(conn, inst, credit_id, meera)
    assert _status(result) == ("matched", "manual")
    assert money(conn, inst, meera)["outstanding"] == 0
    assert {"upi_id": "lakshmi@oksbi", "source": "learned"} in get_member(conn, inst, meera)["upi_ids"]
    # Next month the same payer matches by itself.
    run_billing(conn, inst, date(2026, 7, 1))
    nxt = record_credit(conn, inst, credit(utr="615412345777", upi="lakshmi@oksbi",
                                           at=datetime(2026, 7, 8, tzinfo=IST)))
    assert _status(nxt) == ("matched", "known_upi_id")


def test_assign_without_learning(conn, inst):
    meera = add_member(conn, inst, "Meera")
    credit_id = _queued(conn, inst)
    assign_credit(conn, inst, credit_id, meera, remember_upi_id=False)
    assert get_member(conn, inst, meera)["upi_ids"] == []


def test_a_split_must_add_up(conn, inst):
    diya = add_member(conn, inst)
    kabir = add_member(conn, inst, "Kabir")
    credit_id = _queued(conn, inst, amount="3500")
    for bad in [[(diya, 200000)], [(diya, 200000), (kabir, 200000)], [(diya, 350000), (diya, 0)], []]:
        with pytest.raises(ValueError):
            split_credit(conn, inst, credit_id, bad)
    result = split_credit(conn, inst, credit_id, [(kabir, 150000), (diya, 200000)])
    assert [a["member_id"] for a in result["applied"]] == sorted([diya, kabir])


def test_assigning_an_assigned_credit_again(conn, inst):
    diya = add_member(conn, inst)
    kabir = add_member(conn, inst, "Kabir")
    credit_id = _queued(conn, inst)
    assign_credit(conn, inst, credit_id, diya)
    assert assign_credit(conn, inst, credit_id, diya)["duplicate"] is True     # a retried click
    with pytest.raises(Conflict):
        assign_credit(conn, inst, credit_id, kabir)
    assert money(conn, inst, diya)["paid"] == 200000 and money(conn, inst, kabir)["paid"] == 0


def test_assigning_a_missing_credit_or_member(conn, inst):
    diya = add_member(conn, inst)
    with pytest.raises(NotFound):
        assign_credit(conn, inst, 999999, diya)
    credit_id = _queued(conn, inst)
    with pytest.raises(NotFound):
        assign_credit(conn, inst, credit_id, 999999)


def test_reassign_moves_money_and_both_balances_follow(conn, inst):
    diya = add_member(conn, inst)
    kabir = add_member(conn, inst, "Kabir")
    run_billing(conn, inst, JUNE)
    credit_id = record_credit(conn, inst, credit(note=code(diya)))["credit"]["id"]
    result = reassign_credit(conn, inst, credit_id, [(kabir, 200000)])
    assert result["changed"] is True
    assert money(conn, inst, diya)["outstanding"] == 200000
    assert money(conn, inst, kabir)["outstanding"] == 0
    assert reassign_credit(conn, inst, credit_id, [(kabir, 200000)])["changed"] is False


def test_reassign_lets_the_old_owner_use_other_credit(conn, inst):
    diya = add_member(conn, inst)
    kabir = add_member(conn, inst, "Kabir")
    run_billing(conn, inst, JUNE)
    first = record_credit(conn, inst, credit(utr="615412345601", note=code(diya)))["credit"]["id"]
    record_credit(conn, inst, credit(utr="615412345602", note=code(diya),
                                     at=datetime(2026, 6, 11, tzinfo=IST)))      # Diya now has Rs 2,000 credit
    assert money(conn, inst, diya)["credit"] == 200000
    reassign_credit(conn, inst, first, [(kabir, 200000)])
    # The second payment now pays Diya's June fee.
    assert money(conn, inst, diya) == {"billed": 200000, "paid": 200000, "outstanding": 0, "credit": 0}


def test_reassign_needs_an_assigned_credit(conn, inst):
    diya = add_member(conn, inst)
    credit_id = _queued(conn, inst)
    with pytest.raises(Conflict):
        reassign_credit(conn, inst, credit_id, [(diya, 200000)])


def test_review_queue_lists_waiting_credits_oldest_first(conn, inst):
    add_member(conn, inst)
    _queued(conn, inst, utr="615412345602", at=datetime(2026, 6, 12, tzinfo=IST))
    _queued(conn, inst, utr="615412345601", at=datetime(2026, 6, 11, tzinfo=IST))
    queue = review_queue(conn, inst, date(2026, 6, 20))
    assert [q["utr"] for q in queue] == ["615412345601", "615412345602"]


# --- bugs found by the randomised test ------------------------------------------------------

def test_a_scholarship_sibling_gets_no_zero_share(conn, inst):
    add_member(conn, inst, fee="2000", upi_ids=["sunita@oksbi"])
    add_member(conn, inst, "Kabir", fee="1500", upi_ids=["sunita@oksbi"], concession=100)
    result = record_credit(conn, inst, credit(amount="2000", upi="sunita@oksbi"))
    assert _status(result) == ("needs_review", None)


def test_a_payment_can_return_to_a_charge_it_already_part_paid(conn, inst):
    diya = add_member(conn, inst)
    kabir = add_member(conn, inst, "Kabir")
    run_billing(conn, inst, JUNE)
    p1 = record_credit(conn, inst, credit(utr="615412345601", amount="1500", note=code(diya)))["credit"]["id"]
    record_credit(conn, inst, credit(utr="615412345602", amount="1000", note=code(diya),
                                     at=datetime(2026, 6, 11, tzinfo=IST)))
    # The second payment paid Rs 500 of June and holds Rs 500 credit. Moving the first
    # payment away sends the second back to the June charge it already part-paid.
    reassign_credit(conn, inst, p1, [(kabir, 150000)])
    assert [c["paid"] for c in charges_of(conn, inst, diya)] == [100000]
    with tenant_tx(conn, inst):
        rows = conn.execute("SELECT COUNT(*) AS n FROM allocations a JOIN payments p ON p.id = a.payment_id "
                            "WHERE p.member_id = %s", (diya,)).fetchone()
    assert rows["n"] == 1


def test_siblings_paying_before_they_start_split_by_their_first_fees(conn, inst):
    diya = add_member(conn, inst, fee="2000", start="2026-07", upi_ids=["sunita@oksbi"])
    kabir = add_member(conn, inst, "Kabir", fee="1500", start="2026-07", upi_ids=["sunita@oksbi"])
    result = record_credit(conn, inst, credit(amount="3500", upi="sunita@oksbi"))     # paid in June
    assert _status(result) == ("matched", "sibling_split")
    assert money(conn, inst, diya)["credit"] == 200000 and money(conn, inst, kabir)["credit"] == 150000
