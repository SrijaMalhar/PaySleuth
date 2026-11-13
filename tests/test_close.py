"""Statement continuity and the daily close: noticing when data is missing, not just when it is wrong."""

from datetime import date, datetime
from pathlib import Path

from conftest import add_member, credit
from paysleuth.alerts import list_alerts, resolve_alert
from paysleuth.charges import run_billing
from paysleuth.close import check_credits_landed, list_statements, run_close
from paysleuth.continuity import check_continuity
from paysleuth.db import tenant_tx
from paysleuth.importer import import_statement
from paysleuth.payments import record_credit, reverse_credit
from paysleuth.periods import IST
from paysleuth.statements import parse_statement

SAMPLE = (Path(__file__).parent.parent / "sample_data" / "june_2026_statement.csv").read_text()
HEADER = "Txn Date,Narration,Chq/Ref No,Withdrawal Amt,Deposit Amt,Balance\n"


def _statement(*lines):
    return HEADER + "\n".join(lines) + "\n"


def _line(day, utr, amount, balance, debit=False):
    narration = "ATM WDL" if debit else f"UPI/CR/{utr}/PAYER/SBIN/payer{utr[-3:]}@oksbi/fees"
    return (f'{day:02d}/06/2026,{narration},{utr},"{amount}",,"{balance}"' if debit
            else f'{day:02d}/06/2026,{narration},{utr},,"{amount}","{balance}"')


# --- one statement accounts for itself ------------------------------------------------------

def test_the_sample_statement_accounts_for_itself():
    check = check_continuity(parse_statement(SAMPLE).rows)
    assert check.status == "ok" and check.breaks == []
    assert (check.opening, check.closing) == (5000000, 6360000)
    assert check.opening + check.total_credits - check.total_debits == check.closing
    assert check.repeated_lines == [7]                    # the line the bank printed twice counts once


def test_a_missing_line_is_caught_and_sized():
    lines = SAMPLE.splitlines(keepends=True)
    del lines[3]                                          # drop the Rs 1,800 NEFT credit
    check = check_continuity(parse_statement("".join(lines)).rows)
    assert check.status == "broken"
    [gap] = check.breaks
    assert gap["line"] == 4 and gap["unaccounted"] == "1800.00"
    assert gap["expected_balance"] == "50500.00" and gap["printed_balance"] == "52300.00"


def test_a_line_that_cannot_be_read_leaves_a_hole_that_is_caught():
    text = SAMPLE.replace('"3,500.00","55,500.00"', '"3,5OO.00","55,500.00"')      # letter O, not zero
    parsed = parse_statement(text)
    assert [e["line"] for e in parsed.errors] == [3]
    check = check_continuity(parsed.rows)
    assert check.status == "broken" and check.breaks[0]["unaccounted"] == "3500.00"


def test_a_changed_amount_is_caught():
    check = check_continuity(parse_statement(SAMPLE.replace('"3,500.00","55,500.00"',
                                                            '"3,000.00","55,500.00"')).rows)
    assert check.status == "broken" and check.breaks[0]["line"] == 3


def test_a_change_to_the_very_first_line_only_shows_against_the_previous_statement(pool, conn, inst):
    """The first line has nothing before it in the file, so only the cross-statement check can see it."""
    changed = JUNE_2.replace('"2,000.00","14,000.00"', '"2,400.00","14,000.00"')
    assert check_continuity(parse_statement(changed).rows).status == "ok"
    import_statement(pool, inst, JUNE_1)
    assert import_statement(pool, inst, changed)["statement"]["follows"] == "gap"


def test_a_newest_first_statement_is_read_in_the_right_direction():
    text = _statement(_line(3, "615412345603", "500.00", "11,500.00", debit=True),
                      _line(2, "615412345602", "1,000.00", "12,000.00"),
                      _line(1, "615412345601", "1,000.00", "11,000.00"))
    check = check_continuity(parse_statement(text).rows)
    assert check.status == "ok" and check.newest_first is True
    assert (check.opening, check.closing) == (1000000, 1150000)


def test_a_statement_without_balances_cannot_be_checked():
    text = "Txn Date,Narration,Deposit Amt\n01/06/2026,UPI/CR/615412345601/A/SBIN/a.b@oksbi/x,100\n"
    assert check_continuity(parse_statement(text).rows).status == "no_balance"


# --- one statement follows the one before ---------------------------------------------------

JUNE_1 = _statement(_line(1, "615412345601", "1,000.00", "11,000.00"),
                    _line(2, "615412345602", "1,000.00", "12,000.00"))
JUNE_2 = _statement(_line(3, "615412345603", "2,000.00", "14,000.00"),
                    _line(4, "615412345604", "500.00", "13,500.00", debit=True))
JUNE_3_AFTER_A_MISSING_DAY = _statement(_line(6, "615412345606", "1,000.00", "17,500.00"))


def test_consecutive_statements_must_join_up(pool, conn, inst):
    first = import_statement(pool, inst, JUNE_1)["statement"]
    second = import_statement(pool, inst, JUNE_2)["statement"]
    assert (first["follows"], second["follows"]) == ("first", "continues")
    assert list_alerts(conn, inst) == []

    third = import_statement(pool, inst, JUNE_3_AFTER_A_MISSING_DAY)
    assert third["statement"]["follows"] == "gap"
    assert third["statement"]["gap"]["unaccounted"] == "3000.00"      # 16,500 opening vs 13,500 closing
    assert third["recorded"] == 1                                       # still imported, but flagged
    [alert] = list_alerts(conn, inst)
    assert alert["kind"] == "statement_gap" and "a page or a day is missing" in alert["message"]


def test_reimporting_a_statement_raises_nothing_new(pool, conn, inst):
    import_statement(pool, inst, JUNE_1)
    import_statement(pool, inst, JUNE_3_AFTER_A_MISSING_DAY)
    again = import_statement(pool, inst, JUNE_3_AFTER_A_MISSING_DAY)["statement"]
    assert again["already_imported"] is True
    assert len(list_alerts(conn, inst)) == 1


def test_an_overlapping_statement_is_not_called_a_gap(pool, conn, inst):
    import_statement(pool, inst, JUNE_2)
    earlier = import_statement(pool, inst, JUNE_1)["statement"]         # an older file imported later
    assert earlier["follows"] == "overlap" and list_alerts(conn, inst) == []


def test_a_broken_statement_raises_an_alert_but_still_imports(pool, conn, inst):
    lines = SAMPLE.splitlines(keepends=True)
    del lines[3]
    summary = import_statement(pool, inst, "".join(lines))
    assert summary["statement"]["continuity"] == "broken" and summary["recorded"] == 4
    [alert] = list_alerts(conn, inst)
    assert alert["kind"] == "statement_broken" and alert["details"]["breaks"][0]["unaccounted"] == "1800.00"
    assert resolve_alert(conn, inst, alert["id"])["status"] == "resolved"
    assert list_alerts(conn, inst) == [] and len(list_alerts(conn, inst, status=None)) == 1


def test_accounts_are_checked_separately(pool, conn, inst):
    import_statement(pool, inst, JUNE_1, account="current")
    other = import_statement(pool, inst, JUNE_3_AFTER_A_MISSING_DAY, account="savings")["statement"]
    assert other["follows"] == "first"


# --- every credited rupee is somewhere ------------------------------------------------------

def test_the_close_accounts_for_every_credited_rupee(conn, inst):
    diya = add_member(conn, inst)
    kabir = add_member(conn, inst, "Kabir")
    run_billing(conn, inst, date(2026, 6, 1))
    record_credit(conn, inst, credit(utr="615412345601", amount="2000", note=f"PS{1000 + diya}"))
    record_credit(conn, inst, credit(utr="615412345602", amount="5000", note=f"PS{1000 + kabir}"))
    record_credit(conn, inst, credit(utr="615412345603", amount="700", upi="walkin@ybl"))
    returned = record_credit(conn, inst, credit(utr="615412345604", amount="2000", note=f"PS{1000 + diya}",
                                                at=datetime(2026, 6, 11, tzinfo=IST)))
    reverse_credit(conn, inst, returned["credit"]["id"])
    close = run_close(conn, inst, date(2026, 6, 20))
    assert close["status"] == "clean" and close["books_balance"] is True
    assert (close["credited"], close["applied_to_bills"], close["held_as_advance"],
            close["waiting_for_review"], close["returned_by_bank"], close["unexplained"]) == \
        ("9700.00", "4000.00", "3000.00", "700.00", "2000.00", "0.00")


def test_money_that_does_not_add_up_raises_one_alert(conn, inst):
    diya = add_member(conn, inst)
    waiting = record_credit(conn, inst, credit(upi="walkin@ybl"))["credit"]["id"]
    with tenant_tx(conn, inst):                    # a payment that no code path would write
        conn.execute("INSERT INTO payments (institution_id, bank_credit_id, member_id, amount, received_at) "
                     "VALUES (%s, %s, %s, 50000, now())", (inst, waiting, diya))
    close = run_close(conn, inst, date(2026, 6, 20))
    assert close["status"] == "attention" and close["unexplained"] == "-500.00"
    assert close["mismatched_credits"] == [{"id": waiting, "utr": "615412345601", "status": "needs_review",
                                            "amount": "2000.00", "applied": "500.00"}]
    run_close(conn, inst, date(2026, 6, 20))       # running the close again does not alert again
    [alert] = list_alerts(conn, inst)
    assert alert["kind"] == "books_unbalanced"


def test_an_open_statement_alert_keeps_the_close_from_being_clean(pool, conn, inst):
    import_statement(pool, inst, JUNE_1)
    import_statement(pool, inst, JUNE_3_AFTER_A_MISSING_DAY)
    close = run_close(conn, inst, date(2026, 6, 20))
    assert close["status"] == "attention" and close["books_balance"] is True
    assert close["statements_complete"] is False
    resolve_alert(conn, inst, list_alerts(conn, inst)[0]["id"])       # the missing page was found
    assert run_close(conn, inst, date(2026, 6, 20))["status"] == "clean"


# --- every credit line the statement printed must be in the books ---------------------------

UNIMPORTABLE = _statement(
    _line(1, "615412345601", "2,000.00", "12,000.00"),
    '02/06/2026,ODD TRANSFER 12345,12345,,"8,000.00","20,000.00"',      # UTR too short to accept
    _line(3, "615412345603", "4,000.00", "24,000.00"))


def test_a_credit_line_that_cannot_be_imported_is_reported_as_missing(pool, conn, inst):
    """The bug this guards: the close used to sum the rows that landed, so a line that
    never became a row was invisible on both sides and the close said 'clean'."""
    summary = import_statement(pool, inst, UNIMPORTABLE)
    assert summary["recorded"] == 2
    statement = summary["statement"]
    assert statement["credits_recorded"] == "6000.00" and statement["credits_dropped"] == "8000.00"
    assert statement["credits_missing"] == [{"line": 3, "utr": "12345", "amount": "8000.00"}]
    [alert] = list_alerts(conn, inst)
    assert alert["kind"] == "credits_missing" and "Rs 8000.00" in alert["message"]

    close = run_close(conn, inst, date(2026, 6, 20))
    assert close["status"] == "attention" and close["every_statement_line_imported"] is False
    assert close["dropped_by_import"] == "8000.00"
    assert close["books_balance"] is True          # what did land is still consistent


def test_a_statement_whose_lines_all_landed_is_clean(pool, conn, inst):
    summary = import_statement(pool, inst, SAMPLE)
    statement = summary["statement"]
    assert statement["credits_dropped"] == "0.00" and statement["credits_missing"] == []
    # The line the bank printed twice is one (UTR, amount) pair, so it is neither
    # counted twice nor called missing.
    assert statement["credits_recorded"] == "21800.00" and statement["repeated_lines"] == [7]
    assert list_alerts(conn, inst) == []
    assert run_close(conn, inst, date(2026, 6, 20))["status"] == "clean"


def test_a_masked_upi_id_in_an_unrecognised_layout_no_longer_loses_the_line(pool, conn, inst):
    """The bug this guards: the generic narration parser kept '**verma@okhdfcbank' as
    the payer's UPI ID, which then failed validation and dropped the whole credit."""
    text = _statement(
        '01/06/2026,UPI/CR/615412345601/RAJESH/HDFC/**verma@okhdfcbank/fees,,,"8,000.00","18,000.00"')
    assert parse_statement(text).credits[0].payer_upi_id is None
    summary = import_statement(pool, inst, text)
    assert summary["recorded"] == 1 and summary["statement"]["credits_dropped"] == "0.00"
    assert list_alerts(conn, inst) == []


def test_the_landed_check_asks_the_books_not_the_importer(pool, conn, inst):
    """Deleting a credit behind the importer's back is still caught, because the check
    compares the statement's lines with what is actually stored."""
    import_statement(pool, inst, SAMPLE)
    with tenant_tx(conn, inst):
        statement_id = conn.execute("SELECT id FROM statement_imports").fetchone()["id"]
        conn.execute("DELETE FROM bank_credits WHERE utr = '615412345601'")
    result = check_credits_landed(conn, inst, statement_id, parse_statement(SAMPLE).credits)
    assert result["credits_dropped"] == "2000.00"
    assert [m["utr"] for m in result["credits_missing"]] == ["615412345601"]
    assert [a["kind"] for a in list_alerts(conn, inst)] == ["credits_missing"]


def test_what_the_statement_said_about_itself_is_kept_and_reported(pool, conn, inst):
    """repeated_lines and newest_first are stored and returned, not just computed.

    A line the bank printed twice, and an export that ran newest-first, are both
    things an owner comparing the file with the app would want to see named.
    """
    import_statement(pool, inst, SAMPLE)
    newest_first = _statement(_line(3, "615412349903", "500.00", "11,500.00", debit=True),
                              _line(2, "615412349902", "1,000.00", "12,000.00"),
                              _line(1, "615412349901", "1,000.00", "11,000.00"))
    import_statement(pool, inst, newest_first, account="second")
    first, second = list_statements(conn, inst)
    assert first["repeated_lines"] == [7] and first["newest_first"] is False
    assert second["repeated_lines"] == [] and second["newest_first"] is True
    assert second["opening_balance"] == "10000.00" and second["closing_balance"] == "11500.00"
