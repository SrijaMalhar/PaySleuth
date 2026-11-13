"""One golden file per bank: the adapter's output must equal a saved, known-good result.

The statements in tests/golden are synthetic (see its README). Set
PAYSLEUTH_UPDATE_GOLDEN=1 to regenerate the expected files after replacing a
statement with a real, anonymised export.
"""

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from paysleuth import banks
from paysleuth.banks.common import full_upi_id
from paysleuth.continuity import check_continuity
from paysleuth.money import to_rupees
from paysleuth.banks import canara, hdfc, icici, sbi
from paysleuth.statements import parse_narration, parse_statement

GOLDEN = Path(__file__).parent / "golden"
BANKS = {"sbi": "SBI", "hdfc": "HDFC", "icici": "ICICI", "axis": "Axis", "canara": "Canara"}


def _result(text: str) -> dict:
    parsed = parse_statement(text)
    check = check_continuity(parsed.rows)
    return {
        "bank": parsed.bank,
        "credits": [{**asdict(c), "amount": to_rupees(c.amount), "received_at": c.received_at.isoformat()}
                    for c in parsed.credits],
        "skipped_debits": parsed.skipped_debits,
        "errors": parsed.errors,
        "continuity": check.status,
        "opening_balance": to_rupees(check.opening),
        "closing_balance": to_rupees(check.closing),
    }


@pytest.mark.parametrize("stem, bank", BANKS.items())
def test_each_banks_export_parses_to_its_golden_result(stem, bank):
    actual = _result((GOLDEN / f"{stem}.csv").read_text())
    expected_path = GOLDEN / f"{stem}.expected.json"
    if os.environ.get("PAYSLEUTH_UPDATE_GOLDEN"):
        expected_path.write_text(json.dumps(actual, indent=2) + "\n")
    assert actual == json.loads(expected_path.read_text())
    # Whatever the golden file says, these must hold for every bank.
    assert actual["bank"] == bank
    assert actual["errors"] == [] and actual["continuity"] == "ok"
    assert all(c["channel"] != "OTHER" or "CASH" in c["narration"] for c in actual["credits"])


def test_a_truncated_or_masked_upi_id_is_never_stored():
    assert full_upi_id("Sunita.Sharma@OKSBI") == "sunita.sharma@oksbi"
    for partial in ["sunita.sha", "**sharma@oksbi", "", None, "rk.verma@"]:
        assert full_upi_id(partial) is None
    sbi = _result((GOLDEN / "sbi.csv").read_text())["credits"]
    assert sbi[0]["payer_upi_id"] is None and sbi[1]["payer_upi_id"] == "rk.verma@okhdfcbank"
    assert all(c["payer_upi_id"] is None for c in _result((GOLDEN / "canara.csv").read_text())["credits"])


@pytest.mark.parametrize("narration", [
    # Every narration shape that carries a UPI ID, each with that ID masked by the bank.
    "UPI/CR/615412345601/SUNITA SHARMA/SBIN/**sharma@oksbi/June fees",             # generic slash
    "UPI-SUNITA SHARMA-**sharma@oksbi-SBIN0001234-615412345601-JUNE FEES",         # generic hyphen
    "BY TRANSFER-UPI/CR/615412345601/SUNITA S/SBIN/sunita.sha/June fees--",        # SBI
    "UPI-SUNITA SHARMA-**SHARMA@OKSBI-SBIN0001234-615412345601-JUNE FEES",         # HDFC
    "UPI/615412345601/June fees/**sharma@oksbi/State Bank Of India/ICI0a1b2c3d",   # ICICI
    "UPI/CR/615412345601/SUNITA SHARMA/SBIN/**sharma@oksbi/June fees//CNRB0001234",  # Canara
])
def test_no_narration_shape_keeps_a_masked_upi_id(narration):
    """Not just the helper: every parser that reads a UPI ID must drop a masked one.

    The bug this guards: the generic parser kept the masked string, which then
    failed validation in normalise_upi_id and dropped the whole credit line.
    """
    for parse in (parse_narration, sbi.narration, hdfc.narration, icici.narration, canara.narration):
        parsed = parse(narration)
        if parsed is not None:
            assert parsed["payer_upi_id"] is None, f"{parse.__module__} kept {parsed['payer_upi_id']!r}"
            assert parsed["utr"] == "615412345601"


def test_an_unknown_layout_falls_back_to_the_generic_parser():
    assert banks.detect(["date", "narration", "amount"]) is None
    text = ("Txn Date,Narration,Chq/Ref No,Withdrawal Amt,Deposit Amt,Balance\n"
            "01/06/2026,UPI/CR/615412345601/SUNITA/SBIN/sunita@oksbi/fees,,,100.00,100.00\n")
    parsed = parse_statement(text)
    assert parsed.bank is None and parsed.credits[0].payer_upi_id == "sunita@oksbi"


def test_lines_are_numbered_as_in_the_file_even_after_a_preamble():
    text = (GOLDEN / "sbi.csv").read_text().replace('"2,000.00","52,000.00"', '"2,OOO.00","52,000.00"')
    assert [e["line"] for e in parse_statement(text).errors] == [10]


def test_anonymising_a_statement_keeps_its_shape_and_removes_who_paid():
    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    from anonymise_statement import anonymise

    original = (GOLDEN.parent.parent / "sample_data" / "june_2026_statement.csv").read_text()
    hidden = anonymise(original)
    for secret in ("SUNITA", "VERMA", "LAKSHMI", "sunita.sharma", "615412345601", "meera", "GREEN LEAF"):
        assert secret not in hidden, secret
    assert "PS1004" in hidden and "fee" in hidden                  # reference codes and plain words stay
    before, after = parse_statement(original), parse_statement(hidden)
    assert [(c.amount, c.channel, c.received_at) for c in after.credits] == \
        [(c.amount, c.channel, c.received_at) for c in before.credits]
    assert after.credits[3].utr == after.credits[4].utr            # the repeated line still repeats
    assert check_continuity(after.rows).status == "ok"
    assert anonymise(original) == hidden                           # same input, same output
