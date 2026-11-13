from datetime import date

import pytest

from paysleuth.money import to_paise, to_rupees
from paysleuth.names import first_name, similarity, surname
from paysleuth.periods import check_period, due_date, next_period, period_of, periods_from


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
