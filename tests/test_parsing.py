from datetime import date

import pytest

from paysleuth.money import to_paise, to_rupees
from paysleuth.names import first_name, similarity, surname
from paysleuth.periods import check_period, due_date, next_period, period_of, periods_from
from paysleuth.statements import parse_narration, parse_statement


# --- money ---------------------------------------------------------------------------

def test_money_and_months():
    assert to_paise("2,000.50") == 200050 and to_rupees(200050) == "2000.50"
    with pytest.raises(ValueError):
        to_paise("12.345")
    assert next_period("2026-12") == "2027-01"
    assert periods_from("2026-11", "2027-01") == ["2026-11", "2026-12", "2027-01"]
    assert due_date("2026-02", 31) == date(2026, 2, 28)


@pytest.mark.parametrize("text, paise", [
    ("0.29", 29),            # 0.29 * 100 is 28.999999999999996 as a float
    ("1,00,000", 10000000),  # Indian digit grouping
    (" 3500 ", 350000),
    ("0.01", 1),
    ("9999999999.99", 999999999999),
])
def test_rupees_become_exact_paise(text, paise):
    assert to_paise(text) == paise


@pytest.mark.parametrize("bad", ["0", "0.00", "-5", "abc", "", "1e3", "12.", ".5", "12345678901"])
def test_bad_amounts_are_refused(bad):
    with pytest.raises(ValueError):
        to_paise(bad)


def test_paise_format_back_to_rupees():
    assert to_rupees(5) == "0.05"
    assert to_rupees(-150) == "-1.50"
    assert to_rupees(0) == "0.00"


# --- billing months ---------------------------------------------------------------------

def test_billing_months():
    assert period_of(date(2026, 6, 30)) == "2026-06"
    assert next_period("2026-06") == "2026-07"
    assert periods_from("2026-07", "2026-06") == []
    assert due_date("2028-02", 30) == date(2028, 2, 29)     # leap year
    assert due_date("2026-06", 5) == date(2026, 6, 5)
    for bad in ["2026-13", "2026-6", "26-06", "2026-00", None]:
        with pytest.raises(ValueError):
            check_period(bad)


# --- payer names ------------------------------------------------------------------------

@pytest.mark.parametrize("payer, guardian, low, high", [
    ("R K VERMA", "Rajesh Kumar Verma", 1.0, 1.0),           # initials
    ("MRS SUNITA SHAR", "Sunita Devi Sharma", 1.0, 1.0),     # title dropped, surname cut short
    ("SUNITA SHARMA", "Sunita Sharma", 1.0, 1.0),
    ("ANIL KUMAR", "Rajesh Kumar Verma", 0.2, 0.3),          # one word in common, wrong surname
    ("PRIYA NAIR", "Rajesh Kumar Verma", 0.0, 0.0),
])
def test_payer_names_are_matched_the_way_banks_print_them(payer, guardian, low, high):
    assert low <= similarity(payer, guardian) <= high


def test_name_matching_edge_cases():
    assert similarity("VERMA RAJESH", "Rajesh Kumar Verma") < 1.0    # order matters
    assert similarity("AN VERMA", "Anand Verma") == 0.5              # a 2-letter prefix is not a match
    assert similarity("", "Rajesh Verma") == 0.0
    assert similarity("MR", "Rajesh Verma") == 0.0                   # only a title
    assert first_name("Mrs. Sunita Sharma") == "SUNITA"
    assert first_name(None) is None
    assert surname("Meera Iyer") == "IYER" and surname("Meera") is None


# --- narrations -------------------------------------------------------------------------


def test_slash_style_upi_narration():
    parsed = parse_narration("UPI/CR/615412345601/SUNITA SHARMA/SBIN/sunita.sharma@oksbi/June fees")
    assert parsed == {"channel": "UPI", "utr": "615412345601", "payer_name": "SUNITA SHARMA",
                      "payer_upi_id": "sunita.sharma@oksbi", "note": "June fees"}
    without_bank = parse_narration("UPI/615412345609/ANIL/anil@ybl")
    assert without_bank["utr"] == "615412345609" and without_bank["note"] is None


def test_hyphen_style_upi_narration_with_a_hyphen_inside_the_upi_id():
    parsed = parse_narration("UPI-R K VERMA-rk-verma@okhdfcbank-HDFC0001234-512345678901-FEES PS1002")
    assert parsed["payer_upi_id"] == "rk-verma@okhdfcbank"
    assert parsed["utr"] == "512345678901"
    assert parsed["payer_name"] == "R K VERMA" and parsed["note"] == "FEES PS1002"


def test_neft_and_imps_narrations():
    neft = parse_narration("NEFT-N156260012345-RAJESH KUMAR VERMA-PS1002 advance")
    assert (neft["channel"], neft["utr"], neft["payer_name"], neft["note"]) == \
        ("NEFT", "N156260012345", "RAJESH KUMAR VERMA", "PS1002 advance")
    imps = parse_narration("IMPS/615412345699/PRIYA NAIR")
    assert imps["channel"] == "IMPS" and imps["note"] is None
    assert parse_narration("ATM CASH DEPOSIT") is None
    assert parse_narration("   ") is None


BANK_CSV = """Txn Date,Narration,Chq/Ref No,Withdrawal Amt,Deposit Amt,Balance
01/06/2026,UPI/CR/615412345601/SUNITA SHARMA/SBIN/sunita.sharma@oksbi/June fees,615412345601,,"2,000.00","12,000.00"
02/06/2026,ATM WDL MG ROAD,,500.00,,"11,500.00"
03/06/2026 14:05,CASH DEPOSIT BY SELF,CDM00012345,,1500,"13,000.00"
04/06/2026,UPI/CR/615412345603/MEERA/UBIN/meera@okaxis/fees,615412345603,,12.345,
31/02/2026,UPI/CR/615412345604/ANIL/UBIN/anil@okaxis/fees,615412345604,,2000,
05/06/2026,UPI-R K VERMA-rk.verma@okhdfcbank-HDFC0001234-615412345602-JUNE FEES,615412345602,,"3,500.00","16,500.00"
"""


def test_a_bank_style_csv_becomes_credits_with_bad_lines_reported():
    result = parse_statement(BANK_CSV)
    assert result.skipped_debits == 1
    assert [e["line"] for e in result.errors] == [5, 6]          # bad amount, 31 February
    assert [c.utr for c in result.credits] == ["615412345601", "CDM00012345", "615412345602"]
    cash = result.credits[1]
    assert cash.channel == "OTHER" and cash.amount == 150000 and cash.line == 4
    assert cash.received_at.isoformat() == "2026-06-03T14:05:00+05:30"     # no zone given: Indian time
    assert result.credits[2].amount == 350000 and result.credits[2].payer_name == "R K VERMA"


def test_a_csv_without_the_needed_columns_is_refused():
    with pytest.raises(ValueError, match="credit"):
        parse_statement("Date,Narration,Amount\n01/06/2026,x,100\n")
    with pytest.raises(ValueError):
        parse_statement("")


def test_column_names_are_matched_loosely():
    text = ("Transaction Date,Transaction Remarks,Withdrawal Amount (INR ),Deposit Amount (INR )\n"
            "2026-06-01,UPI/CR/615412345601/SUNITA/SBIN/s@oksbi/x,0.00,100.00\n")
    result = parse_statement(text)
    assert len(result.credits) == 1 and result.credits[0].amount == 10000
