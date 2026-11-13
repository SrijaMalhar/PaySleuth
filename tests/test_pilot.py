"""The pilot tool: the path a real institution's statement takes, end to end.

This is the tool that produces the only numbers worth quoting, so it is tested
like anything else. The review step reads from stdin, so the tests script it.
"""

from pathlib import Path

import pytest

from conftest import DSN
from paysleuth.db import tenant_tx
from paysleuth.money import to_rupees
from tools.pilot import main as pilot

MEMBERS = """name,guardian_name,monthly_fee,phone,upi_ids,billing_day,start_period,concession_pct
Diya Sharma,Sunita Sharma,2000,9876543210,sunita.sharma@oksbi,5,2026-06,0
Rohan Verma,Rajesh Kumar Verma,2000,9876500001,rk.verma@okhdfcbank,5,2026-06,0
Kabir Verma,Rajesh Kumar Verma,1500,9876500001,rk.verma@okhdfcbank,5,2026-06,0
Arjun Nair,Anil Nair,1800,,,5,2026-06,0
Meera Iyer,Lakshmi Iyer,2500,9876500004,,5,2026-06,25
"""

# Shaped like a real PG's export: two notes empty, one payer name truncated by the
# bank, one sibling transfer, one stranger, one debit.
STATEMENT = """Txn Date,Narration,Chq/Ref No,Withdrawal Amt,Deposit Amt,Balance
01/06/2026,UPI/CR/615412340001/SUNITA SHARMA/SBIN/sunita.sharma@oksbi/,615412340001,,"2,000.00","42,000.00"
02/06/2026,UPI-R K VERMA-rk.verma@okhdfcbank-HDFC0001234-615412340002-FEES,615412340002,,"3,500.00","45,500.00"
03/06/2026,UPI/CR/615412340003/ANIL NAIR/SBIN/anil.nair@oksbi/june rent,615412340003,,"1,800.00","47,300.00"
04/06/2026,ATM WDL MG ROAD,,"5,000.00",,"42,300.00"
05/06/2026,UPI/CR/615412340004/LAKSHMI IYER/UBIN/lakshmi.iyer@oksbi/meera,615412340004,,"1,875.00","44,175.00"
06/06/2026,UPI/CR/615412340006/RAMESH K/SBIN/ramesh.k@oksbi/,615412340006,,"1,800.00","45,975.00"
"""


@pytest.fixture
def pilot_in(pool, tmp_path, capsys):
    """Run pilot subcommands against a test schema, returning captured output."""
    dsn = f"{DSN}?options=-c%20search_path%3D{pool.schema}"
    members = tmp_path / "members.csv"
    members.write_text(MEMBERS)
    statement = tmp_path / "june.csv"
    statement.write_text(STATEMENT)

    def run(*args, answers=None):
        if answers is not None:
            import builtins
            replies = iter(answers)
            original = builtins.input

            def fake_input(prompt=""):
                print(prompt, end="")
                return next(replies)
            builtins.input = fake_input
            try:
                code = pilot(["--database-url", dsn, *args])
            finally:
                builtins.input = original
        else:
            code = pilot(["--database-url", dsn, *args])
        assert code == 0
        captured = capsys.readouterr()
        run.err = captured.err
        return captured.out

    run.members, run.statement = str(members), str(statement)
    return run


def test_setup_creates_the_members_from_a_csv(pilot_in, conn):
    out = pilot_in("setup", "--name", "Sunrise PG", "--members", pilot_in.members, "--late-fee", "100")
    assert "5 of 5 members added" in out
    assert "PS1001  Diya Sharma  Rs 2000.00" in out and "api_key=ps_" in out
    assert "institution_id=1" in out
    with tenant_tx(conn, 1):
        rows = conn.execute("SELECT name, concession_pct FROM members ORDER BY id").fetchall()
        shared = conn.execute("SELECT COUNT(*) AS n FROM member_upi_ids "
                              "WHERE upi_id = 'rk.verma@okhdfcbank'").fetchone()
    assert [r["name"] for r in rows] == ["Diya Sharma", "Rohan Verma", "Kabir Verma",
                                          "Arjun Nair", "Meera Iyer"]
    assert rows[4]["concession_pct"] == 25
    assert shared["n"] == 2                  # the siblings share their father's UPI ID


def test_a_bad_row_is_reported_and_the_rest_still_load(pilot_in, tmp_path):
    broken = tmp_path / "broken.csv"
    broken.write_text(MEMBERS.replace("Arjun Nair,Anil Nair,1800", "Arjun Nair,Anil Nair,eighteen hundred"))
    out = pilot_in("setup", "--name", "Sunrise PG", "--members", str(broken))
    assert "4 of 5 members added" in out
    assert "line 5: skipped" in pilot_in.err


def test_a_members_file_missing_a_required_column_is_refused(pilot_in, tmp_path):
    thin = tmp_path / "thin.csv"
    thin.write_text("name,phone\nDiya,9876543210\n")
    with pytest.raises(SystemExit, match="monthly_fee"):
        pilot_in("setup", "--name", "X", "--members", str(thin))


def test_import_reports_what_matched_and_what_did_not(pilot_in):
    pilot_in("setup", "--name", "Sunrise PG", "--members", pilot_in.members)
    out = pilot_in("import", "--institution", "1", "--statement", pilot_in.statement,
                   "--as-of", "2026-06-30")
    assert "billed 5 charges for 5 members" in out
    # Diya by her registered UPI ID, the Vermas split by their fees, and three left.
    assert "recorded 5 credits: 2 matched automatically, 3 waiting for a person" in out
    assert "debits skipped 1" in out
    assert "statement: continuity ok, follows first" in out
    assert "dropped Rs 0.00" in out and "daily close: clean" in out


def test_review_records_each_decision_against_what_was_suggested(pilot_in, conn):
    pilot_in("setup", "--name", "Sunrise PG", "--members", pilot_in.members)
    pilot_in("import", "--institution", "1", "--statement", pilot_in.statement, "--as-of", "2026-06-30")
    out = pilot_in("review", "--institution", "1", "--as-of", "2026-06-30",
                   answers=["1", "PS1005", "s"])
    assert "3 credit(s) waiting" in out
    outcomes_shown = [line.split(":")[-1].strip() for line in out.splitlines() if "q to stop:" in line]
    assert outcomes_shown == ["applied", "applied", "skipped"]
    assert "2 resolved, 1 skipped this session" in out

    with tenant_tx(conn, 1):
        outcomes = conn.execute("SELECT chosen, rank_of_choice, candidates FROM review_outcomes "
                                "ORDER BY id").fetchall()
    assert len(outcomes) == 2
    # The first accepted the top suggestion, so its rank is 1 and the signals that
    # were on screen are kept with it.
    assert outcomes[0]["rank_of_choice"] == 1
    assert outcomes[0]["candidates"][0]["signals"]
    # The second named a member by reference code instead of taking a suggestion.
    assert outcomes[1]["chosen"] == [5]


def test_a_split_entered_by_hand_is_applied(pilot_in, conn):
    pilot_in("setup", "--name", "Sunrise PG", "--members", pilot_in.members)
    pilot_in("import", "--institution", "1", "--statement", pilot_in.statement, "--as-of", "2026-06-30")
    out = pilot_in("review", "--institution", "1", "--as-of", "2026-06-30",
                   answers=["PS1001 1000 PS1004 800", "q"])
    assert "applied" in out
    with tenant_tx(conn, 1):
        parts = conn.execute(
            """SELECT m.ref_code, p.amount FROM payments p JOIN members m ON m.id = p.member_id
                JOIN bank_credits b ON b.id = p.bank_credit_id WHERE b.utr = '615412340003'
                ORDER BY m.ref_code""").fetchall()
    assert [(r["ref_code"], to_rupees(r["amount"])) for r in parts] == \
        [("PS1001", "1000.00"), ("PS1004", "800.00")]


def test_a_split_that_does_not_add_up_is_rejected_without_stopping(pilot_in):
    pilot_in("setup", "--name", "Sunrise PG", "--members", pilot_in.members)
    pilot_in("import", "--institution", "1", "--statement", pilot_in.statement, "--as-of", "2026-06-30")
    out = pilot_in("review", "--institution", "1", "--as-of", "2026-06-30",
                   answers=["PS1001 5 PS1004 5", "PS1004", "q"])
    assert "not applied: the shares add up to Rs 10.00" in out
    assert "applied" in out                   # the next credit was still offered
    assert "1 resolved" in out


def test_an_unknown_reference_code_does_not_crash_the_session(pilot_in):
    pilot_in("setup", "--name", "Sunrise PG", "--members", pilot_in.members)
    pilot_in("import", "--institution", "1", "--statement", pilot_in.statement, "--as-of", "2026-06-30")
    out = pilot_in("review", "--institution", "1", "--as-of", "2026-06-30", answers=["PS9999", "q"])
    assert "not applied" in out and "0 resolved" in out


def test_measure_prints_the_numbers_and_refuses_to_tune_on_too_few(pilot_in):
    pilot_in("setup", "--name", "Sunrise PG", "--members", pilot_in.members)
    pilot_in("import", "--institution", "1", "--statement", pilot_in.statement, "--as-of", "2026-06-30")
    pilot_in("review", "--institution", "1", "--as-of", "2026-06-30", answers=["1", "1", "s"])
    out = pilot_in("measure", "--institution", "1")
    assert "credits recorded                  5" in out
    assert "matched without a person          2  (40.0%)" in out
    assert "decisions measured                2" in out
    assert "top suggestion was the choice     100.0%" in out
    assert "note_empty_pct                     40.0%" in out
    assert "48 more resolved credits before the weights can be tuned" in out
    assert "Quote these numbers and no others" in out


def test_measure_on_an_institution_with_nothing_says_not_measured(pilot_in):
    pilot_in("setup", "--name", "Sunrise PG", "--members", pilot_in.members)
    out = pilot_in("measure", "--institution", "1")
    assert "matched without a person          0  (not measured yet)" in out
    assert "top suggestion was the choice     not measured yet" in out


def test_the_members_template_in_the_repository_loads(pilot_in):
    template = Path(__file__).parent.parent / "sample_data" / "members_template.csv"
    out = pilot_in("setup", "--name", "Sunrise PG", "--members", str(template))
    assert "5 of 5 members added" in out


def test_real_data_paths_cannot_be_committed_by_accident():
    """The gitignore rules that keep a real institution's statement out of the repository."""
    rules = (Path(__file__).parent.parent / ".gitignore").read_text()
    for pattern in ("*_real.csv", "real_*.csv", "pilot_data/"):
        assert pattern in rules


def test_the_pilot_help_warns_against_measuring_anonymised_data():
    """The methodological trap: anonymising first would score the anonymiser."""
    import tools.pilot

    assert "Do not anonymise first if you intend to measure" in tools.pilot.__doc__
