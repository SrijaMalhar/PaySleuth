from datetime import date

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege

from conftest import SUPERUSER_DSN, add_member, credit
from paysleuth.charges import run_billing
from paysleuth.db import check_role, tenant_tx
from paysleuth.institutions import create_institution, institution_for_key
from paysleuth.matching import REVIEW, candidate_members, decide, ref_codes_in
from paysleuth.payments import record_credit, review_queue


def _raw_member(conn, institution_id, ref_code="PS9001"):
    with tenant_tx(conn, institution_id):
        return conn.execute(
            "INSERT INTO members (institution_id, ref_code, name, billing_day, start_period) "
            "VALUES (%s, %s, 'Diya', 5, '2026-06') RETURNING id", (institution_id, ref_code)
        ).fetchone()["id"]


# --- tenancy ------------------------------------------------------------------------

def test_another_institution_cannot_see_members_even_without_a_where_clause(conn, inst):
    _raw_member(conn, inst)
    other, _ = create_institution(conn, "Other Centre")
    with tenant_tx(conn, inst):
        assert conn.execute("SELECT COUNT(*) AS n FROM members").fetchone()["n"] == 1
    with tenant_tx(conn, other):
        assert conn.execute("SELECT COUNT(*) AS n FROM members").fetchone()["n"] == 0


def test_with_no_institution_set_nothing_is_visible(conn, inst):
    _raw_member(conn, inst)
    assert conn.execute("SELECT COUNT(*) AS n FROM members").fetchone()["n"] == 0
    with conn.transaction():          # a transaction that never set the tenant
        assert conn.execute("SELECT COUNT(*) AS n FROM members").fetchone()["n"] == 0


def test_cannot_write_rows_for_another_institution(conn, inst):
    other, _ = create_institution(conn, "Other Centre")
    with pytest.raises(InsufficientPrivilege):
        with tenant_tx(conn, other):
            conn.execute("INSERT INTO audit_log (institution_id, action, entity) "
                         "VALUES (%s, 'x', 'y')", (inst,))


def test_api_keys_are_stored_only_as_hashes(conn):
    institution_id, key = create_institution(conn, "Bright Minds Tuition")
    assert key.startswith("ps_")
    assert institution_for_key(conn, key) == institution_id
    assert institution_for_key(conn, key + "x") is None
    assert institution_for_key(conn, None) is None
    stored = conn.execute("SELECT api_key_hash FROM staff WHERE institution_id = %s",
                          (institution_id,)).fetchone()["api_key_hash"]
    assert len(stored) == 64 and all(ch in "0123456789abcdef" for ch in stored)
    assert key not in stored


@pytest.mark.skipif(not SUPERUSER_DSN, reason="set PAYSLEUTH_TEST_SUPERUSER_URL to run")
def test_refuses_to_run_as_a_role_that_bypasses_row_level_security():
    with psycopg.connect(SUPERUSER_DSN, row_factory=psycopg.rows.dict_row) as su:
        with pytest.raises(RuntimeError, match="bypasses row-level security"):
            check_role(su)


def test_the_app_role_is_accepted(conn):
    check_role(conn)


# --- matching rules ---------------------------------------------------------------------


JUNE = date(2026, 6, 20)


def _decide(conn, inst, amount, upi=None, note=None):
    with tenant_tx(conn, inst):
        return decide(conn, amount, upi, note, JUNE)


def test_reference_codes_are_found_in_notes():
    assert ref_codes_in("fees PS1001 and ps-1002, PS 1003") == ["PS1001", "PS1002", "PS1003"]
    assert ref_codes_in("APS1001 PS12") == []
    assert ref_codes_in(None) == []


def test_one_code_picks_that_member(conn, inst):
    diya = add_member(conn, inst)
    decision = _decide(conn, inst, 123400, note=f"june PS{1000 + diya}")
    assert decision.parts == ((diya, 123400),) and decision.rule == "ref_code"


def test_two_codes_with_the_exact_total_split_by_what_each_owes(conn, inst):
    diya = add_member(conn, inst, fee="2000")
    kabir = add_member(conn, inst, "Kabir", fee="1500")
    run_billing(conn, inst, JUNE)
    note = f"PS{1000 + diya} PS{1000 + kabir}"
    assert _decide(conn, inst, 350000, note=note).parts == ((diya, 200000), (kabir, 150000))
    assert _decide(conn, inst, 300000, note=note) == REVIEW          # not the exact total


def test_a_shared_upi_id_with_the_exact_fees_total_splits(conn, inst):
    diya = add_member(conn, inst, fee="2000", upi_ids=["sunita@oksbi"])
    kabir = add_member(conn, inst, "Kabir", fee="1500", upi_ids=["sunita@oksbi"])
    decision = _decide(conn, inst, 350000, upi="sunita@oksbi")        # nothing billed yet: by fees
    assert decision.parts == ((diya, 200000), (kabir, 150000)) and decision.rule == "sibling_split"
    assert _decide(conn, inst, 200000, upi="sunita@oksbi") == REVIEW  # which child? a person decides


def test_a_scholarship_sibling_never_gets_a_zero_share(conn, inst):
    add_member(conn, inst, fee="2000", upi_ids=["sunita@oksbi"])
    add_member(conn, inst, "Kabir", fee="1500", upi_ids=["sunita@oksbi"], concession=100)
    assert _decide(conn, inst, 200000, upi="sunita@oksbi") == REVIEW


def test_an_unknown_code_falls_back_to_the_upi_id(conn, inst):
    diya = add_member(conn, inst, upi_ids=["sunita@oksbi"])
    with tenant_tx(conn, inst):
        assert candidate_members(conn, "sunita@oksbi", "PS9999") == ([diya], "upi_id")
    assert _decide(conn, inst, 200000, upi="sunita@oksbi", note="PS9999").rule == "known_upi_id"


def test_anything_else_goes_to_review(conn, inst):
    add_member(conn, inst)
    assert _decide(conn, inst, 200000, upi="stranger@ybl", note="fees") == REVIEW
    assert _decide(conn, inst, 200000) == REVIEW


def test_codes_from_another_institution_do_not_match(conn, inst):
    other, _ = create_institution(conn, "Other Centre")
    stranger = add_member(conn, other)
    assert _decide(conn, inst, 200000, note=f"PS{1000 + stranger}") == REVIEW


# --- suggestions for the review queue ------------------------------------------------------

def _queue_one(conn, inst, **kw):
    record_credit(conn, inst, credit(**kw))
    queue = review_queue(conn, inst, JUNE)
    assert len(queue) == 1 and queue[0]["status"] == "needs_review"      # suggestions are never applied
    return queue[0]["suggestions"]


def test_a_first_name_in_the_note_ranks_that_child_first(conn, inst):
    add_member(conn, inst, "Diya Sharma", guardian="Sunita Sharma")
    meera = add_member(conn, inst, "Meera Iyer", guardian="Lakshmi Iyer")
    run_billing(conn, inst, JUNE)
    top = _queue_one(conn, inst, upi="lakshmi.iyer@oksbi", name="LAKSHMI IYER", note="meera fees")[0]
    assert top["member_id"] == meera
    assert "note mentions Meera" in top["reasons"] and "payer name matches Lakshmi Iyer" in top["reasons"]
    assert top["score"] == 40 + 35 + 10 + 30          # outstanding, guardian, surname, note


def test_a_cut_short_bank_name_still_matches_the_guardian(conn, inst):
    add_member(conn, inst, "Diya", guardian="Sunita Devi Sharma", fee="2000")
    rohan = add_member(conn, inst, "Rohan", guardian="Rajesh Kumar Verma", fee="2500")
    run_billing(conn, inst, JUNE)
    top = _queue_one(conn, inst, amount="2500", upi="rkv@ybl", name="R K VERMA")[0]
    assert top["member_id"] == rohan and top["score"] == 40 + 35


def test_amount_alone_is_a_weak_signal_and_ties_are_stable(conn, inst):
    diya = add_member(conn, inst, "Diya", fee="2000")
    kabir = add_member(conn, inst, "Kabir", fee="2000")
    suggestions = _queue_one(conn, inst, amount="6000", upi="walkin@ybl")      # three months of either fee
    assert [(s["member_id"], s["score"]) for s in suggestions] == [(diya, 15), (kabir, 15)]
    assert suggestions[0]["reasons"] == ["amount is exactly 3 months of fees"]


def test_a_shared_upi_id_suggests_the_family_and_the_matching_fee_first(conn, inst):
    diya = add_member(conn, inst, "Diya", fee="2000", upi_ids=["sunita@oksbi"])
    kabir = add_member(conn, inst, "Kabir", fee="1500", upi_ids=["sunita@oksbi"])
    arjun = add_member(conn, inst, "Arjun", fee="1500")
    run_billing(conn, inst, JUNE)
    suggestions = _queue_one(conn, inst, amount="1500", upi="sunita@oksbi")
    # Kabir: amount and family (60). Arjun: amount only (40). Diya: family only (20).
    assert [(s["member_id"], s["score"]) for s in suggestions] == [(kabir, 60), (arjun, 40), (diya, 20)]
    assert suggestions[2]["reasons"] == ["this UPI ID already pays for the family"]


def test_no_signal_means_no_suggestion(conn, inst):
    add_member(conn, inst, "Diya", fee="2000")
    assert _queue_one(conn, inst, amount="777", upi="walkin@ybl", name="PRIYA NAIR") == []
