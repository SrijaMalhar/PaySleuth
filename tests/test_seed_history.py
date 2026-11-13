"""The multi-month seeder, and the four bank layouts it writes.

The layouts are the part worth testing: each one is generated to match a real
adapter's header and narration, so if an adapter's regex changes and the
generator is not updated, the statements silently fall back to the generic
parser or stop parsing altogether. That happened twice while writing it.
"""

from datetime import date

import pytest

from conftest import DSN
from paysleuth.db import tenant_tx
from paysleuth.statements import parse_statement
from tools.seed_history import LAYOUTS, Statement, main as seed_history, shift


@pytest.mark.parametrize("bank, detected, has_upi_id, has_name", [
    ("HDFC", "HDFC", True, True),        # prints both
    ("SBI", "SBI", False, True),         # truncates the UPI ID away, shortens the name
    ("ICICI", "ICICI", True, False),     # the ID but never the payer's name
    ("AXIS", "Axis", False, True),       # the name but no ID at all
])
def test_each_layout_is_read_by_the_adapter_it_imitates(bank, detected, has_upi_id, has_name):
    statement = Statement(bank, 4_000_000)
    statement.credit(date(2026, 6, 5), "Sunita Sharma", "sunita.sharma@oksbi",
                     "615412340001", "PS1001", 200_000)
    statement.debit(date(2026, 6, 7), "ATM WDL MG ROAD", 500_000)
    parsed = parse_statement(statement.text())

    assert parsed.bank == detected, "fell back to the generic parser"
    assert not parsed.errors
    assert parsed.skipped_debits == 1
    credit = parsed.credits[0]
    assert credit.utr == "615412340001"
    assert credit.note == "PS1001"
    assert bool(credit.payer_upi_id) is has_upi_id
    assert bool(credit.payer_name) is has_name


def test_every_layout_keeps_the_running_balance_honest():
    """A generated statement has to account for itself, or every close is noise."""
    for bank in LAYOUTS:
        statement = Statement(bank, 1_000_000)
        statement.credit(date(2026, 6, 5), "A Payer", "a.payer@oksbi", "615412340001", "", 200_000)
        statement.debit(date(2026, 6, 6), "ATM WDL", 50_000)
        statement.credit(date(2026, 6, 7), "B Payer", "b.payer@oksbi", "615412340002", "", 300_000)
        from paysleuth.continuity import check_continuity
        assert check_continuity(parse_statement(statement.text()).rows).status == "ok", bank
        assert statement.balance == 1_000_000 + 200_000 - 50_000 + 300_000


@pytest.mark.parametrize("period, months, expected", [
    ("2026-06", -3, "2026-03"), ("2026-01", -1, "2025-12"),
    ("2026-12", 1, "2027-01"), ("2026-06", 0, "2026-06"),
])
def test_shift_crosses_year_boundaries(period, months, expected):
    assert shift(period, months) == expected


def test_a_short_history_seeds_and_accounts_for_itself(pool, conn, capsys):
    """Two months end to end: every close clean, nothing printed but unrecorded."""
    assert seed_history([
        "--database-url", f"{DSN}?options=-c%20search_path%3D{pool.schema}",
        "--months", "2", "--members", "12",
    ]) == 0
    out = capsys.readouterr().out
    assert "12 members in" in out
    assert out.count("close clean") == 2, out
    assert "The data is invented" in out

    with tenant_tx(conn, 1):
        missing = conn.execute("SELECT COUNT(*) AS n FROM alerts "
                               "WHERE kind = 'credits_missing'").fetchone()["n"]
        dropped = conn.execute("SELECT COALESCE(SUM(credits_dropped), 0) AS s "
                               "FROM statement_imports").fetchone()["s"]
        charges = conn.execute("SELECT COUNT(*) AS n FROM charges").fetchone()["n"]
        credits = conn.execute("SELECT COUNT(*) AS n FROM bank_credits").fetchone()["n"]
    assert missing == 0, "a statement printed a credit that never became a row"
    assert dropped == 0
    assert charges > 0 and credits > 0


def test_the_seeder_says_the_data_is_invented():
    """The warning is load-bearing: tune_weights will happily score this."""
    import tools.seed_history

    assert "THE DATA IS INVENTED" in tools.seed_history.__doc__
    assert "tools/pilot.py" in tools.seed_history.__doc__
