"""Metrics and masked logs."""

import io
import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import DSN, add_member, credit
from paysleuth.alerts import list_alerts
from paysleuth.charges import run_billing
from paysleuth.close import run_close
from paysleuth.db import tenant_tx
from paysleuth.importer import import_statement
from paysleuth.institutions import create_institution
from paysleuth.logging_setup import MaskPersonalData, configure, mask
from paysleuth.metrics import collect, render
from paysleuth.outbox import MAX_ATTEMPTS, relay_once
from paysleuth.payments import record_credit
from paysleuth.periods import IST
from paysleuth.reminders import queue_reminders
from tools.seed_demo import main as seed_demo

NOW = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)


def _values(samples, name, **labels):
    return [v for n, lbl, v in samples if n == name and all(lbl.get(k) == str(x) for k, x in labels.items())]


# --- metrics -----------------------------------------------------------------------------

def test_the_numbers_an_operator_watches(conn, inst):
    diya = add_member(conn, inst, phone="9876543210")
    add_member(conn, inst, "Kabir")
    run_billing(conn, inst, date(2026, 6, 1))
    record_credit(conn, inst, credit(utr="615412345601", note=f"PS{1000 + diya}"))
    record_credit(conn, inst, credit(utr="615412345602", upi="walkin@ybl",
                                     at=datetime(2026, 6, 18, 12, 0, tzinfo=IST)))
    queue_reminders(conn, inst, date(2026, 6, 20))
    samples = collect(conn, NOW)

    assert _values(samples, "paysleuth_credits_total", status="matched") == [1]
    assert _values(samples, "paysleuth_credits_total", status="needs_review") == [1]
    assert _values(samples, "paysleuth_credits_matched_total", rule="ref_code") == [1]
    assert _values(samples, "paysleuth_review_queue_depth") == [1]
    [age] = _values(samples, "paysleuth_review_queue_oldest_seconds")
    assert age == pytest.approx(timedelta(days=2, hours=5.5).total_seconds(), abs=60)
    assert _values(samples, "paysleuth_outbox_depth", status="pending") == [2]      # receipt + reminder
    assert _values(samples, "paysleuth_members_active") == [2]
    assert _values(samples, "paysleuth_outstanding_paise") == [200000]              # Kabir's June fee
    assert _values(samples, "paysleuth_approvals_pending") == [0]
    assert _values(samples, "paysleuth_last_close_clean") == []                     # no close has run


def test_outbox_lag_and_dead_messages_are_visible(conn, inst):
    diya = add_member(conn, inst)
    record_credit(conn, inst, credit(note=f"PS{1000 + diya}"))
    with tenant_tx(conn, inst):                      # a message queued two hours ago
        conn.execute("UPDATE outbox SET created_at = %s, next_attempt_at = %s",
                     (NOW - timedelta(hours=2), NOW - timedelta(hours=2)))
    [lag] = _values(collect(conn, NOW), "paysleuth_outbox_oldest_pending_seconds")
    assert lag == pytest.approx(7200, abs=5)

    def always_fails(payload):
        raise ConnectionError("gateway down")

    moment = NOW
    for _ in range(MAX_ATTEMPTS):
        relay_once(conn, inst, always_fails, now=lambda: moment)
        moment += timedelta(hours=2)
    samples = collect(conn, moment)
    assert _values(samples, "paysleuth_outbox_dead_total") == [1]
    assert _values(samples, "paysleuth_outbox_oldest_pending_seconds") == []        # nothing waiting now


def test_a_broken_statement_and_a_clean_close_show_up(pool, conn, inst):
    text = ("Txn Date,Narration,Chq/Ref No,Withdrawal Amt,Deposit Amt,Balance\n"
            '01/06/2026,UPI/CR/615412345601/A/SBIN/a.b@oksbi/fees,615412345601,,"1,000.00","11,000.00"\n'
            '02/06/2026,UPI/CR/615412345602/C/SBIN/c.d@oksbi/fees,615412345602,,"1,000.00","15,000.00"\n')
    import_statement(pool, inst, text)
    assert [a["kind"] for a in list_alerts(conn, inst)] == ["statement_broken"]
    run_close(conn, inst, date(2026, 6, 20))
    samples = collect(conn, NOW)
    assert _values(samples, "paysleuth_alerts_open", kind="statement_broken") == [1]
    assert _values(samples, "paysleuth_last_close_clean") == [0]       # an open statement alert


def test_metrics_are_reported_per_institution(conn, inst):
    other, _ = create_institution(conn, "Other Centre")
    add_member(conn, inst, "Diya")
    samples = collect(conn, NOW)
    assert _values(samples, "paysleuth_members_active", institution=inst) == [1]
    assert _values(samples, "paysleuth_members_active", institution=other) == [0]


def test_the_prometheus_text_format():
    text = render([("paysleuth_review_queue_depth", {"institution": "1"}, 3),
                   ("paysleuth_review_queue_depth", {"institution": "2"}, 0),
                   ("paysleuth_outbox_oldest_pending_seconds", {"institution": "1"}, 12.5),
                   ("paysleuth_unknown_series", {}, 1)])
    assert text == (
        "# HELP paysleuth_review_queue_depth Credits waiting for a person\n"
        "# TYPE paysleuth_review_queue_depth gauge\n"
        'paysleuth_review_queue_depth{institution="1"} 3\n'
        'paysleuth_review_queue_depth{institution="2"} 0\n'
        "# HELP paysleuth_outbox_oldest_pending_seconds Age of the oldest message still waiting\n"
        "# TYPE paysleuth_outbox_oldest_pending_seconds gauge\n"
        'paysleuth_outbox_oldest_pending_seconds{institution="1"} 12.5\n')
    assert 'kind="a\\"b"' in render([("paysleuth_alerts_open", {"kind": 'a"b'}, 1)])


# --- masked logs --------------------------------------------------------------------------

@pytest.mark.parametrize("before, after", [
    ("sent receipt to 9876543210", "sent receipt to 98****3210"),
    ("phone=+91 98765 43210", "phone=98****3210"),      # the country code goes too
    ("phone=09876543210", "phone=98****3210"),
    ("payer sunita.sharma@oksbi paid", "payer su****@oksbi paid"),
    ("rk-verma@okhdfcbank", "rk****@okhdfcbank"),
    ("UTR 615412345601 for Rs 2000.00", "UTR 615412345601 for Rs 2000.00"),   # not a phone number
    ("member 1234567890 billed", "member 1234567890 billed"),                 # does not start 6-9
    ("statement line 12 of 40", "statement line 12 of 40"),
])
def test_phone_numbers_and_upi_ids_are_masked(before, after):
    assert mask(before) == after


def test_masking_leaves_enough_to_tell_two_parents_apart():
    assert mask("9876543210") != mask("9876549999")
    assert "9876" not in mask("9876543210").replace("98", "", 1)


def test_the_filter_masks_whatever_a_log_line_interpolates():
    stream = io.StringIO()
    configure(logging.INFO, stream)
    log = logging.getLogger("paysleuth.test")
    log.info("receipt for %s to %s", "sunita.sharma@oksbi", "9876543210")
    log.info("payload=%s", {"phone": "9876543210", "upi": "rk.verma@okhdfcbank"})
    log.warning("no personal data here")
    logging.getLogger("some.library").info("gateway replied for 9876543210")     # not our logger
    lines = stream.getvalue().splitlines()
    assert "su****@oksbi" in lines[0] and "98****3210" in lines[0]
    assert "sunita.sharma@oksbi" not in stream.getvalue() and "9876543210" not in stream.getvalue()
    assert "98****3210" in lines[1] and "rk****@okhdfcbank" in lines[1]
    assert lines[2].endswith("no personal data here")
    assert "98****3210" in lines[3]


def test_a_broken_format_string_does_not_lose_the_record():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s %s", ("only one",), None)
    assert MaskPersonalData().filter(record) is True


def test_metrics_through_the_api(api):
    api.client.post("/members", json={"name": "Diya", "monthly_fee": "2000", "billing_day": 5,
                                      "start_period": "2026-06"})
    response = api.client.get("/metrics", headers={"X-API-Key": "none"})          # no key needed
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "paysleuth_members_active{institution=" in response.text
    assert "# TYPE paysleuth_review_queue_depth gauge" in response.text


def test_the_demo_seed_runs_and_leaves_a_believable_month(pool, capsys):
    """The seed is what an interviewer clicks through, so it is tested like anything else."""
    assert seed_demo(["--database-url", f"{DSN}?options=-c%20search_path%3D{pool.schema}"]) == 0
    printed = capsys.readouterr().out
    assert "owner_api_key=ps_" in printed and "clerk_api_key=ps_" in printed
    assert "3 matched automatically, 2 waiting for a person" in printed
    assert "statement continuity: ok" in printed and "daily close: clean" in printed
    with pool.connection() as conn:
        samples = collect(conn, NOW)
    assert _values(samples, "paysleuth_members_active") == [5]
    assert _values(samples, "paysleuth_review_queue_depth") == [2]
    assert _values(samples, "paysleuth_last_close_clean") == [1]
