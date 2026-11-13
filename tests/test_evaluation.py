"""Measuring matching, and tuning the suggestion weights against recorded decisions."""

from datetime import date, datetime

from conftest import add_member, credit
from paysleuth.charges import run_billing
from paysleuth.evaluation import (cross_validate, load_outcomes, matching_report, signal_stats, top1_hits,
                                  top_pick, tune)
from paysleuth.matching import WEIGHTS
from paysleuth.payments import assign_credit, record_credit
from paysleuth.periods import IST

NOW = datetime(2026, 6, 12, 10, 0, tzinfo=IST)


def test_each_review_decision_is_recorded_next_to_what_was_suggested(conn, inst):
    add_member(conn, inst, "Diya Sharma", guardian="Sunita Sharma")
    meera = add_member(conn, inst, "Meera Iyer", guardian="Lakshmi Iyer", fee="2500")
    arjun = add_member(conn, inst, "Arjun Nair", guardian="Anil Nair", fee="2500")
    run_billing(conn, inst, date(2026, 6, 1))
    right = record_credit(conn, inst, credit(utr="615412345601", amount="2500", upi="lakshmi@oksbi",
                                             name="LAKSHMI IYER", note="meera fees"))["credit"]["id"]
    wrong = record_credit(conn, inst, credit(utr="615412345602", amount="2500", upi="uncle@ybl",
                                             name="SURESH IYER", note="fees"))["credit"]["id"]
    assign_credit(conn, inst, right, meera)          # the top suggestion was right
    assign_credit(conn, inst, wrong, arjun)          # the uncle paid for Arjun; Meera was suggested first
    first, second = load_outcomes(conn, inst)
    assert first["chosen"] == [meera] and first["rank_of_choice"] == 1
    assert first["candidates"][0] == {"member_id": meera, "signals": [
        "amount_equals_outstanding", "payer_matches_guardian", "same_surname", "note_names_member"]}
    assert second["chosen"] == [arjun] and second["rank_of_choice"] is not None and second["rank_of_choice"] > 1

    report = matching_report(conn, inst, NOW)
    assert report["suggestions"] == {"decisions_measured": 2, "top_suggestion_right_pct": "50.0",
                                     "choice_in_top_3_pct": "100.0", "choice_was_not_suggested": 0}


def test_the_matching_report_measures_rates_and_what_payers_send(conn, inst):
    diya = add_member(conn, inst, upi_ids=["sunita@oksbi"])
    run_billing(conn, inst, date(2026, 6, 1))
    record_credit(conn, inst, credit(utr="615412345601", note=f"PS{1000 + diya}"))
    record_credit(conn, inst, credit(utr="615412345602", upi="sunita@oksbi"))
    record_credit(conn, inst, credit(utr="615412345603", upi="walkin@ybl", note="fees",
                                     at=datetime(2026, 6, 10, 10, 0, tzinfo=IST)))
    record_credit(conn, inst, credit(utr="615412345604", channel="OTHER"))
    report = matching_report(conn, inst, NOW)
    assert (report["credits"], report["auto_matched"], report["waiting_for_review"]) == (4, 2, 2)
    assert report["auto_match_rate_pct"] == "50.0"
    assert report["by_rule"] == {"known_upi_id": 1, "ref_code": 1}
    assert report["oldest_waiting_hours"] == 48.0
    assert report["what_payers_send"] == {"note_empty_pct": "50.0", "note_has_reference_code_pct": "25.0",
                                          "no_usable_upi_id_pct": "50.0", "no_payer_name_pct": "100.0"}


def test_with_nothing_measured_the_report_says_so_instead_of_inventing_a_rate(conn, inst):
    report = matching_report(conn, inst, NOW)
    assert report["auto_match_rate_pct"] is None
    assert report["suggestions"]["top_suggestion_right_pct"] is None


def _outcome(chosen, *candidates):
    return {"chosen": [chosen], "rank_of_choice": None,
            "candidates": [{"member_id": m, "signals": list(signals)} for m, signals in candidates]}


def test_weights_are_replayed_against_recorded_decisions():
    outcome = _outcome(2, (1, ["amount_equals_outstanding"]), (2, ["note_names_member", "same_surname"]))
    assert top_pick(outcome, WEIGHTS) == 1             # 40 against 30 + 10: a tie goes to the lower id
    assert top_pick(outcome, {"amount_equals_outstanding": 40, "note_names_member": 5, "same_surname": 5}) == 1
    assert top_pick(outcome, {"amount_equals_outstanding": 10, "note_names_member": 30}) == 2
    assert top_pick(_outcome(1, (1, [])), WEIGHTS) is None


def test_tuning_moves_a_weight_only_when_the_decisions_say_so():
    # People keep choosing the member named in the note over the one whose amount matches.
    outcomes = [_outcome(2, (1, ["amount_equals_outstanding"]), (2, ["note_names_member"]))] * 30
    assert top1_hits(outcomes, WEIGHTS) == 0
    weights, hits = tune(outcomes)
    assert hits == 30 and weights["note_names_member"] > weights["amount_equals_outstanding"]
    assert {k for k in weights if weights[k] != WEIGHTS[k]} <= {"amount_equals_outstanding", "note_names_member"}
    assert cross_validate(outcomes) == {"held_out": 30, "current_top1_pct": "0.0", "tuned_top1_pct": "100.0"}

    agreeing = [_outcome(1, (1, ["amount_equals_outstanding"]), (2, ["same_surname"]))] * 30
    assert tune(agreeing) == (WEIGHTS, 30)             # nothing to learn: weights untouched
    stats = signal_stats(outcomes)
    assert stats["note_names_member"]["precision_pct"] == "100.0"
    assert stats["amount_equals_outstanding"]["precision_pct"] == "0.0"
