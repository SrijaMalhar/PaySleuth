import random
from datetime import date

import pytest

from conftest import fresh_pool
from factory import books, build_institution, messy_statement
from paysleuth.importer import _DisjointSet, import_statement
from paysleuth.alerts import list_alerts
from paysleuth.db import tenant_tx
from paysleuth.institutions import create_institution
from paysleuth.money import to_rupees
from paysleuth.payments import record_credit, reverse_credit
from paysleuth.statements import parse_statement


def _institution(pool, seed):
    """The same institution, built the same way, in whichever schema the pool points at."""
    rng = random.Random(seed)
    with pool.connection() as conn:
        inst, _ = create_institution(conn, "Bright Minds Tuition")
        families = build_institution(conn, inst, rng, pairs=20, singles=20, bill_through=date(2026, 7, 1))
    return inst, families, rng


def _serial(pool, inst, text):
    """Line by line on one connection, in statement order, then the returned credits.

    The same two phases import_statement uses: every credit first, then the
    reversals, since a line can return a credit the file reported earlier.
    """
    parsed = parse_statement(text)
    with pool.connection() as conn:
        for credit in sorted(parsed.credits, key=lambda c: (c.received_at, c.line)):
            try:
                record_credit(conn, inst, credit)
            except Exception:          # conflicts and bad lines are part of the messy statement
                pass
        for reversal in sorted(parsed.reversals, key=lambda r: (r.reversed_at, r.line)):
            with tenant_tx(conn, inst):
                found = conn.execute("SELECT id, amount FROM bank_credits WHERE utr = %s",
                                     (reversal.utr,)).fetchone()
            if found and found["amount"] == reversal.amount:
                reverse_credit(conn, inst, found["id"], reason="returned by the bank",
                               reference=reversal.narration, reversed_at=reversal.reversed_at)


def _books(pool, inst):
    with pool.connection() as conn:
        return books(conn, inst)


@pytest.mark.parametrize("seed, workers", [(1, 8), (2, 8), (3, 8), (4, 1), (4, 4), (4, 16)])
def test_parallel_import_gives_exactly_the_books_of_a_serial_import(seed, workers):
    """Every worker count is compared with a line-by-line import, not with another parallel run."""
    with fresh_pool(max_size=workers + 4) as serial_pool, fresh_pool(max_size=workers + 4) as parallel_pool:
        inst_s, families, rng = _institution(serial_pool, seed)
        inst_p, _, _ = _institution(parallel_pool, seed)
        text = messy_statement(families, rng, rounds=2)

        _serial(serial_pool, inst_s, text)
        summary = import_statement(parallel_pool, inst_p, text, workers=workers)

        assert summary["recorded"] > 40 and summary["duplicates"] > 0 and summary["conflicts"] == 1
        assert summary["skipped_debits"] == 1 and summary["reversals"] == 3
        assert summary["groups"] > 1
        serial, parallel = _books(serial_pool, inst_s), _books(parallel_pool, inst_p)
        for part in ("credits", "payments", "allocations", "messages", "reversals"):
            assert parallel[part] == serial[part], f"{part} differs at {workers} workers"


def test_reimporting_an_overlapping_statement_changes_nothing(pool):
    inst, families, rng = _institution(pool, 7)
    text = messy_statement(families, rng)
    first = import_statement(pool, inst, text, workers=4)
    before = _books(pool, inst)
    again = import_statement(pool, inst, text, workers=4)
    assert again["recorded"] == 0
    assert again["duplicates"] == first["recorded"] + first["duplicates"]
    assert _books(pool, inst) == before


def test_bad_lines_are_reported_with_line_numbers_and_the_rest_import(pool):
    with pool.connection() as conn:
        inst, _ = create_institution(conn, "Bright Minds Tuition")
    text = ("Txn Date,Narration,Chq/Ref No,Withdrawal Amt,Deposit Amt\n"
            "01/06/2026,UPI/CR/615412345601/SUNITA/SBIN/sunita@oksbi/fees,,,2000\n"
            "02/06/2026,UPI/CR/615412345602/ANIL/SBIN/anil@oksbi/fees,,,abc\n"
            "03/06/2026,CASH DEPOSIT,,,500\n"
            "04/06/2026,UPI/CR/615412345604/X/SBIN/bad upi@x/fees,,,100\n"
            "05/06/2026,UPI/CR/615412345605/RAVI/SBIN/ravi@oksbi/fees,,,1500\n")
    summary = import_statement(pool, inst, text)
    assert summary["recorded"] == 2
    assert [p["line"] for p in summary["problems"]] == [3, 4, 5]
    assert summary["errors"] == 3


def test_every_statement_line_is_accounted_for_after_an_import(pool):
    """What the statement says it credited, minus what could not be imported, is what landed."""
    inst, families, rng = _institution(pool, 13)
    text = messy_statement(families, rng)
    summary = import_statement(pool, inst, text, workers=4)
    statement = summary["statement"]
    assert statement["continuity"] == "no_balance"        # the generated statement prints no balance
    # The statement repeats one UTR with a different amount. That Rs 1.00 line is
    # money the statement claims and the books do not have, so it counts as dropped
    # and raises an alert rather than being quietly ignored.
    assert summary["conflicts"] == 1 and statement["credits_dropped"] == "1.00"
    with pool.connection() as conn:
        assert [a["kind"] for a in list_alerts(conn, inst)] == ["credits_missing"]
        with tenant_tx(conn, inst):
            landed = conn.execute("SELECT COALESCE(SUM(amount), 0) AS n FROM bank_credits").fetchone()["n"]
    assert to_rupees(landed) == statement["credits_recorded"]


def test_union_find_joins_everything_connected():
    sets = _DisjointSet()
    sets.union(("utr", "1"), ("member", 7))
    sets.union(("utr", "2"), ("member", 8))
    assert sets.find(("utr", "1")) != sets.find(("utr", "2"))
    sets.union(("utr", "3"), ("member", 7))
    sets.union(("utr", "3"), ("member", 8))          # a sibling transfer joins both children
    assert sets.find(("utr", "1")) == sets.find(("utr", "2")) == sets.find(("member", 8))
    assert sets.find(("utr", "9")) == ("utr", "9")     # an unseen node is its own set
