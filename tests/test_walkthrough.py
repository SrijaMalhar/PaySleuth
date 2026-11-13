"""The demo in the README, run as a test.

The GIF in the README is a recording of tools/walkthrough.py, so if that script
stops working the README starts lying. This asserts the five beats still happen
and that the last one -- the statement line that never became a row -- is still
caught, because that beat is the project's headline claim and it was false once
already.
"""

import re

from conftest import DSN
from tools.walkthrough import main as walkthrough

ANSI = re.compile(r"\033\[[0-9;]*m")


def plain(text):
    return ANSI.sub("", text)


def test_the_readme_demo_runs_and_reaches_all_five_beats(capsys):
    assert walkthrough(["--database-url", DSN]) == 0
    out = plain(capsys.readouterr().out)

    assert "[1/5] Bill the month" in out
    assert "[2/5] Import the bank's own CSV" in out
    assert "[3/5] What a person actually sees" in out
    assert "[4/5] The same credit, delivered 20 times at once" in out
    assert "[5/5] A statement line that never became a row" in out

    # Beat 2: the three rules that decide without a person, and the two that refuse.
    assert "5 credits recorded, 1 debit skipped" in out
    assert "3 matched with nobody involved, 2 sent to a person" in out
    for rule in ("known_upi_id", "ref_code", "sibling_split"):
        assert rule in out

    # Beat 3: a suggestion is explained in words, not just scored.
    assert "PS1005 Meera Iyer" in out
    assert "amount equals the Rs 1875.00 monthly fee" in out      # 2500 less a 25% concession

    # Beat 4: twenty concurrent deliveries of one UTR leave one row and one payment.
    assert "rows for that UTR: 1" in out
    assert "applied to Diya: Rs 2000.00" in out

    # Beat 5: the alert that the close could not raise until it asked the books.
    assert "Rs 12000.00 dropped" in out
    assert "ALERT credits_missing" in out
    assert "the books have no row for it" in out


def test_the_walkthrough_leaves_no_schema_behind(conn):
    """It writes into its own schema so it can be run against a database with data in it."""
    before = {r["nspname"] for r in conn.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'walkthrough%'").fetchall()}
    assert walkthrough(["--database-url", DSN]) == 0
    after = {r["nspname"] for r in conn.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'walkthrough%'").fetchall()}
    assert after == before
