from datetime import date, datetime

import pytest

from conftest import add_member, charges_of, check_books, credit, money
from paysleuth.charges import run_billing
from paysleuth.db import tenant_tx
from paysleuth.errors import Conflict
from paysleuth.importer import import_statement
from paysleuth.outbox import relay_once
from paysleuth.payments import assign_credit, reassign_credit, record_credit, reverse_credit, review_queue
from paysleuth.periods import IST
from paysleuth.statements import parse_reversal, parse_statement

JUNE = date(2026, 6, 1)


def code(member_id):
    return f"PS{1000 + member_id}"


def _notices(conn, inst):
    with tenant_tx(conn, inst):
        return [r["payload"] for r in conn.execute(
            "SELECT payload FROM outbox WHERE kind = 'payment_reversed' ORDER BY id").fetchall()]


def test_a_reversal_reopens_the_charges_and_tells_the_parent(conn, inst):
    diya = add_member(conn, inst, phone="9876543210")
    run_billing(conn, inst, JUNE)
    credit_id = record_credit(conn, inst, credit(note=code(diya)))["credit"]["id"]
    result = reverse_credit(conn, inst, credit_id, reason="returned by the bank")
    assert result["credit"]["status"] == "reversed"
    assert result["reopened"] == [{"member_id": diya, "ref_code": code(diya), "amount": "2000.00",
                                   "outstanding_after": "2000.00"}]
    assert money(conn, inst, diya) == {"billed": 200000, "paid": 0, "outstanding": 200000, "credit": 0}
    [notice] = _notices(conn, inst)
    assert notice["utr"] == "615412345601" and notice["phone"] == "9876543210"
    assert check_books(conn, inst) == []


def test_a_reversal_is_recorded_as_a_new_row_and_nothing_is_erased(conn, inst):
    diya = add_member(conn, inst)
    run_billing(conn, inst, JUNE)
    credit_id = record_credit(conn, inst, credit(note=code(diya)))["credit"]["id"]
    reverse_credit(conn, inst, credit_id, reference="UPI/REV/615412345601")
    with tenant_tx(conn, inst):
        assert [r["amount"] for r in conn.execute("SELECT amount FROM payments ORDER BY id")] == [200000, -200000]
        reversal = conn.execute("SELECT amount, reference FROM credit_reversals").fetchone()
        audit = conn.execute("SELECT action FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert reversal == {"amount": 200000, "reference": "UPI/REV/615412345601"}
    assert audit["action"] == "credit.reversed"


def test_reversing_twice_changes_nothing(conn, inst):
    diya = add_member(conn, inst)
    credit_id = record_credit(conn, inst, credit(note=code(diya)))["credit"]["id"]
    reverse_credit(conn, inst, credit_id)
    assert reverse_credit(conn, inst, credit_id)["duplicate"] is True
    assert len(_notices(conn, inst)) == 1
    # A re-sent alert for the returned transfer does not bring the money back.
    assert record_credit(conn, inst, credit(note=code(diya)))["duplicate"] is True
    assert money(conn, inst, diya)["paid"] == 0


def test_after_a_reversal_other_credit_covers_what_reopened(conn, inst):
    diya = add_member(conn, inst)
    run_billing(conn, inst, JUNE)
    first = record_credit(conn, inst, credit(utr="615412345601", note=code(diya)))["credit"]["id"]
    record_credit(conn, inst, credit(utr="615412345602", note=code(diya), at=datetime(2026, 6, 12, tzinfo=IST)))
    assert money(conn, inst, diya)["credit"] == 200000
    result = reverse_credit(conn, inst, first)
    assert result["reopened"][0]["outstanding_after"] == "0.00"
    assert money(conn, inst, diya) == {"billed": 200000, "paid": 200000, "outstanding": 0, "credit": 0}


def test_a_reversed_sibling_split_reopens_both_children(conn, inst):
    diya = add_member(conn, inst, fee="2000", upi_ids=["sunita@oksbi"])
    kabir = add_member(conn, inst, "Kabir", fee="1500", upi_ids=["sunita@oksbi"])
    run_billing(conn, inst, JUNE)
    credit_id = record_credit(conn, inst, credit(amount="3500", upi="sunita@oksbi"))["credit"]["id"]
    reverse_credit(conn, inst, credit_id)
    assert [c["paid"] for m in (diya, kabir) for c in charges_of(conn, inst, m)] == [0, 0]
    assert [n["member_id"] for n in _notices(conn, inst)] == [diya, kabir]


def test_a_reversed_credit_leaves_the_review_queue_and_cannot_be_assigned(conn, inst):
    diya = add_member(conn, inst)
    credit_id = record_credit(conn, inst, credit(upi="walkin@ybl"))["credit"]["id"]
    reverse_credit(conn, inst, credit_id)
    assert review_queue(conn, inst, JUNE) == []
    assert _notices(conn, inst) == []                       # nobody was credited, so nobody to tell
    with pytest.raises(Conflict):
        assign_credit(conn, inst, credit_id, diya)
    with pytest.raises(Conflict):
        reassign_credit(conn, inst, credit_id, [(diya, 200000)])


def test_the_reversal_notice_is_delivered_through_the_outbox(conn, inst):
    diya = add_member(conn, inst)
    credit_id = record_credit(conn, inst, credit(note=code(diya)))["credit"]["id"]
    reverse_credit(conn, inst, credit_id)
    sent = []
    relay_once(conn, inst, sent.append, now=lambda: datetime(2030, 1, 15, 10, 30, tzinfo=IST))
    assert sorted(m["kind"] for m in sent) == ["payment_receipt", "payment_reversed"]


def test_reversal_debits_are_recognised_in_narrations():
    assert parse_reversal("UPI/REV/615412345601/SUNITA SHARMA") == "615412345601"
    assert parse_reversal("REV-UPI-615412345601-sunita@oksbi") == "615412345601"
    assert parse_reversal("UPI RETURN 615412345601") == "615412345601"
    assert parse_reversal("ATM WDL MG ROAD") is None
    assert parse_reversal("REVATHI STORES 615412345601") is None          # a word that starts with REV
    assert parse_reversal("UPI/DR/615412345601/RETAIL SHOP") is None


STATEMENT = """Txn Date,Narration,Chq/Ref No,Withdrawal Amt,Deposit Amt,Balance
01/06/2026,UPI/CR/615412345601/SUNITA SHARMA/SBIN/sunita.sharma@oksbi/June fees,615412345601,,"2,000.00",
02/06/2026,UPI/CR/615412345602/ANIL NAIR/SBIN/anil.nair@oksbi/June fees,615412345602,,"1,800.00",
03/06/2026,UPI/REV/615412345601/SUNITA SHARMA,615412345601,"2,000.00",,
03/06/2026,UPI/REV/615412345602/ANIL NAIR,615412345602,"1,000.00",,
03/06/2026,UPI/REV/615412349999/NOBODY,615412349999,"500.00",,
04/06/2026,ATM WDL,,"700.00",,
"""


def test_a_statement_with_returned_credits_reverses_them(pool, conn, inst):
    diya = add_member(conn, inst, upi_ids=["sunita.sharma@oksbi"])
    arjun = add_member(conn, inst, "Arjun", fee="1800", upi_ids=["anil.nair@oksbi"])
    run_billing(conn, inst, JUNE)
    parsed = parse_statement(STATEMENT)
    assert len(parsed.reversals) == 3 and parsed.skipped_debits == 1
    summary = import_statement(pool, inst, STATEMENT)
    assert summary["matched"] == 2 and summary["reversals"] == 1
    assert [p["line"] for p in summary["problems"]] == [5, 6]      # wrong amount; unknown UTR
    assert money(conn, inst, diya)["outstanding"] == 200000
    assert money(conn, inst, arjun)["outstanding"] == 0
    again = import_statement(pool, inst, STATEMENT)                # re-import: nothing new
    assert again["reversals"] == 0 and again["recorded"] == 0
    assert check_books(conn, inst) == []
