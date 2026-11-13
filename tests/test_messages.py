import json
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import add_member, credit
from paysleuth.charges import apply_late_fees, run_billing
from paysleuth.db import tenant_tx
from paysleuth.notify import file_sender, webhook_sender
from paysleuth.outbox import (MAX_ATTEMPTS, _claim, _finish, backoff, in_quiet_hours, next_allowed,
                              relay_once)
from paysleuth.payments import record_credit
from paysleuth.periods import IST
from paysleuth.reminders import queue_reminders
from paysleuth.worker import run_forever

# Messages get next_attempt_at = the database's now(), so the relay's fake clock
# is set after the real one. 10:30 in the morning, Indian time.
T0 = datetime(2030, 1, 15, 5, 0, tzinfo=timezone.utc)


def code(member_id):
    return f"PS{1000 + member_id}"


def _pay(conn, inst, member_id, utr, amount="2000"):
    return record_credit(conn, inst, credit(utr=utr, amount=amount, note=code(member_id)))


def _status(conn, inst):
    with tenant_tx(conn, inst):
        return [(r["kind"], r["status"], r["attempts"]) for r in
                conn.execute("SELECT kind, status, attempts FROM outbox ORDER BY id").fetchall()]


class Gateway:
    """A fake send function that records payloads and can be told to fail."""

    def __init__(self, fail_times=0):
        self.sent = []
        self.fail_times = fail_times
        self.lock = threading.Lock()

    def __call__(self, payload):
        with self.lock:
            if self.fail_times > 0:
                self.fail_times -= 1
                raise ConnectionError("gateway down")
            self.sent.append(payload)


# --- receipts ------------------------------------------------------------------------------

def test_a_receipt_is_delivered_once_with_its_details(conn, inst):
    diya = add_member(conn, inst, phone="9876543210")
    run_billing(conn, inst, date(2026, 6, 1))
    _pay(conn, inst, diya, "615412345601")
    gateway = Gateway()
    assert relay_once(conn, inst, gateway, now=lambda: T0)["sent"] == 1
    assert relay_once(conn, inst, gateway, now=lambda: T0 + timedelta(minutes=5))["claimed"] == 0
    [receipt] = gateway.sent
    assert receipt["kind"] == "payment_receipt" and receipt["utr"] == "615412345601"
    assert receipt["phone"] == "9876543210" and receipt["amount"] == "2000.00"
    assert receipt["covered"] == [{"period": "2026-06", "kind": "monthly_fee", "amount": "2000.00"}]
    assert _status(conn, inst) == [("payment_receipt", "sent", 0)]


def test_failed_sends_retry_with_backoff_then_park_as_dead(conn, inst):
    diya = add_member(conn, inst)
    _pay(conn, inst, diya, "615412345601")
    gateway = Gateway(fail_times=100)
    t = T0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        summary = relay_once(conn, inst, gateway, now=lambda: t)
        assert summary["claimed"] == 1
        # Not ready again until the backoff has passed.
        assert relay_once(conn, inst, gateway, now=lambda: t + backoff(attempt) - timedelta(seconds=1))[
            "claimed"] == 0
        t = t + backoff(attempt)
    assert summary["dead"] == 1
    assert _status(conn, inst) == [("payment_receipt", "dead", MAX_ATTEMPTS)]
    assert relay_once(conn, inst, gateway, now=lambda: t + timedelta(days=1))["claimed"] == 0


def test_backoff_doubles_and_is_capped_at_an_hour():
    waits = [backoff(n).total_seconds() for n in range(1, MAX_ATTEMPTS)]   # the waits actually used
    assert waits[:4] == [5, 10, 20, 40]
    assert waits[-1] == 3600, "the cap must be reached before a message is parked, or it is dead code"
    assert max(waits) == 3600 and sum(waits) == 8715                       # about 2 hours 25 minutes


def test_a_members_receipts_arrive_in_payment_order_even_while_one_is_retried(conn, inst):
    diya = add_member(conn, inst)
    _pay(conn, inst, diya, "615412345601")
    _pay(conn, inst, diya, "615412345602")
    gateway = Gateway(fail_times=1)
    first = relay_once(conn, inst, gateway, now=lambda: T0)
    assert first["claimed"] == 1 and first["failed"] == 1           # the second waits behind the first
    assert relay_once(conn, inst, gateway, now=lambda: T0 + timedelta(seconds=1))["claimed"] == 0
    relay_once(conn, inst, gateway, now=lambda: T0 + timedelta(seconds=5))
    relay_once(conn, inst, gateway, now=lambda: T0 + timedelta(seconds=6))
    assert [r["utr"] for r in gateway.sent] == ["615412345601", "615412345602"]


def test_other_members_are_not_held_up_by_one_failing_message(conn, inst):
    diya = add_member(conn, inst)
    kabir = add_member(conn, inst, "Kabir")
    _pay(conn, inst, diya, "615412345601")
    _pay(conn, inst, kabir, "615412345602")
    gateway = Gateway(fail_times=1)
    summary = relay_once(conn, inst, gateway, now=lambda: T0)
    assert summary["failed"] == 1 and summary["sent"] == 1 and gateway.sent[0]["member_id"] == kabir


def test_a_crashed_workers_claimed_messages_are_picked_up_after_60_seconds(conn, inst):
    diya = add_member(conn, inst)
    _pay(conn, inst, diya, "615412345601")
    with tenant_tx(conn, inst):                       # a worker claimed it, then died before sending
        conn.execute("UPDATE outbox SET status = 'sending', claimed_at = %s", (T0,))
    gateway = Gateway()
    assert relay_once(conn, inst, gateway, now=lambda: T0 + timedelta(seconds=30))["claimed"] == 0
    assert relay_once(conn, inst, gateway, now=lambda: T0 + timedelta(seconds=61))["sent"] == 1


def test_a_slow_worker_cannot_overwrite_the_new_owners_state(conn, inst):
    diya = add_member(conn, inst)
    _pay(conn, inst, diya, "615412345601")
    [slow] = _claim(conn, inst, T0, 10)
    [fast] = _claim(conn, inst, T0 + timedelta(seconds=61), 10)       # the claim expired; re-claimed
    assert _finish(conn, inst, fast, "sent", T0) is True
    assert _finish(conn, inst, slow, "dead", T0) is False             # the old claim is ignored
    assert _status(conn, inst) == [("payment_receipt", "sent", 0)]


def test_many_workers_deliver_every_message_exactly_once(pool, conn, inst):
    members = [add_member(conn, inst, f"Child {i}") for i in range(30)]
    for i, member_id in enumerate(members):
        _pay(conn, inst, member_id, f"6154123456{i:02d}")
    gateway = Gateway()

    def work():
        with pool.connection() as c:
            while relay_once(c, inst, gateway, now=lambda: T0, batch=3)["claimed"]:
                pass

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [p["message_id"] for p in gateway.sent]
    assert len(ids) == 30 and len(set(ids)) == 30


# --- reminders -----------------------------------------------------------------------------

def _reminders(gateway):
    return [(p["period"], p["stage"], p["amount_due"]) for p in gateway.sent if p["kind"] == "overdue_reminder"]


def test_reminders_go_out_on_days_1_5_and_10_once_each(conn, inst):
    diya = add_member(conn, inst, billing_day=5)                      # due June 5
    run_billing(conn, inst, date(2026, 6, 1))
    gateway = Gateway()
    sent_by_day = {}
    for day in range(5, 20):
        queue_reminders(conn, inst, date(2026, 6, day))
        queue_reminders(conn, inst, date(2026, 6, day))               # running twice changes nothing
        relay_once(conn, inst, gateway, now=lambda: T0)
        sent_by_day[day] = len(_reminders(gateway))
    assert [p["stage"] for p in gateway.sent] == [1, 5, 10]
    assert sent_by_day[5] == 0 and sent_by_day[6] == 1 and sent_by_day[10] == 2 and sent_by_day[15] == 3
    assert gateway.sent[0]["pay_with_note"] == f"{code(diya)} 2026-06"


def test_a_late_job_sends_only_the_latest_stage(conn, inst):
    add_member(conn, inst, billing_day=5)
    run_billing(conn, inst, date(2026, 6, 1))
    queue_reminders(conn, inst, date(2026, 6, 16))                    # the job did not run for 11 days
    gateway = Gateway()
    relay_once(conn, inst, gateway, now=lambda: T0)
    assert [p["stage"] for p in gateway.sent] == [10]


def test_no_reminder_when_the_fee_is_paid_and_none_for_late_fees(conn, inst):
    diya = add_member(conn, inst, billing_day=5)
    run_billing(conn, inst, date(2026, 6, 1))
    apply_late_fees(conn, inst, date(2026, 6, 10))
    record_credit(conn, inst, credit(amount="2000", note=code(diya)))  # pays June; the late fee is left
    assert queue_reminders(conn, inst, date(2026, 6, 20)) == 0


def test_reminders_wait_for_morning_during_quiet_hours(conn, inst):
    add_member(conn, inst, billing_day=5)
    run_billing(conn, inst, date(2026, 6, 1))
    queue_reminders(conn, inst, date(2026, 6, 6))
    night = datetime(2030, 1, 15, 22, 30, tzinfo=IST)
    gateway = Gateway()
    assert relay_once(conn, inst, gateway, now=lambda: night)["deferred"] == 1
    assert relay_once(conn, inst, gateway, now=lambda: night + timedelta(hours=9))["claimed"] == 0
    morning = datetime(2030, 1, 16, 8, 0, tzinfo=IST)
    assert relay_once(conn, inst, gateway, now=lambda: morning)["sent"] == 1


def test_receipts_are_not_held_for_quiet_hours(conn, inst):
    diya = add_member(conn, inst)
    _pay(conn, inst, diya, "615412345601")
    gateway = Gateway()
    assert relay_once(conn, inst, gateway, now=lambda: datetime(2030, 1, 15, 23, 0, tzinfo=IST))["sent"] == 1


def test_a_queued_reminder_is_dropped_if_the_fee_was_paid_or_a_later_one_exists(conn, inst):
    diya = add_member(conn, inst, billing_day=5)
    kabir = add_member(conn, inst, "Kabir", billing_day=5)
    run_billing(conn, inst, date(2026, 6, 1))
    queue_reminders(conn, inst, date(2026, 6, 6))                     # stage 1 for both
    queue_reminders(conn, inst, date(2026, 6, 10))                    # stage 5 for both, before any send
    record_credit(conn, inst, credit(amount="2000", note=code(kabir)))  # Kabir pays
    gateway = Gateway()
    for _ in range(4):
        relay_once(conn, inst, gateway, now=lambda: T0)
    reminders = [(p["member_id"], p["stage"]) for p in gateway.sent if p["kind"] == "overdue_reminder"]
    assert reminders == [(diya, 5)]


def test_a_reminder_shows_what_is_left_after_a_part_payment(conn, inst):
    diya = add_member(conn, inst, billing_day=5)
    run_billing(conn, inst, date(2026, 6, 1))
    queue_reminders(conn, inst, date(2026, 6, 6))
    record_credit(conn, inst, credit(amount="500", note=code(diya)))
    gateway = Gateway()
    relay_once(conn, inst, gateway, now=lambda: T0)
    relay_once(conn, inst, gateway, now=lambda: T0)
    assert _reminders(gateway) == [("2026-06", 1, "1500.00")]


def test_quiet_hours_arithmetic():
    assert in_quiet_hours(22, 21, 8) and in_quiet_hours(3, 21, 8) and not in_quiet_hours(8, 21, 8)
    assert in_quiet_hours(14, 13, 15) and not in_quiet_hours(15, 13, 15)
    assert not in_quiet_hours(5, 8, 8)
    assert next_allowed(datetime(2030, 1, 15, 23, 0, tzinfo=IST), 8) == datetime(2030, 1, 16, 8, 0, tzinfo=IST)
    assert next_allowed(datetime(2030, 1, 15, 3, 0, tzinfo=IST), 8) == datetime(2030, 1, 15, 8, 0, tzinfo=IST)


# --- senders -------------------------------------------------------------------------------

@pytest.fixture
def http_gateway():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append({"headers": dict(self.headers), "body": json.loads(body)})
            status = 500 if received[-1]["body"].get("fail") else 200
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/messages", received
    server.shutdown()


def test_the_webhook_sender_posts_json_with_the_message_id(http_gateway):
    url, received = http_gateway
    send = webhook_sender(url)
    send({"message_id": "m-1", "kind": "payment_receipt", "amount": "2000.00"})
    assert received[0]["body"]["amount"] == "2000.00"
    assert received[0]["headers"]["X-Message-Id"] == "m-1"
    assert received[0]["headers"]["Content-Type"] == "application/json"
    with pytest.raises(Exception):
        send({"message_id": "m-2", "fail": True})                     # a 500 is a failure to retry


def test_the_file_sender_writes_one_line_per_message(tmp_path):
    path = tmp_path / "messages.jsonl"
    send = file_sender(str(path))
    threads = [threading.Thread(target=send, args=({"message_id": str(i)},)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = path.read_text().splitlines()
    assert len(lines) == 20 and {json.loads(line)["message_id"] for line in lines} == {str(i) for i in range(20)}


def test_the_worker_runs_the_daily_jobs_and_relays(pool, conn, inst):
    add_member(conn, inst, billing_day=5)
    gateway = Gateway()
    run_forever(pool, gateway, today=lambda: date(2026, 6, 20), sleep=lambda seconds: None, iterations=2)
    kinds = [kind for kind, _, _ in _status(conn, inst)]
    assert kinds == ["overdue_reminder"]                  # billed, late fee added, one reminder queued
    with tenant_tx(conn, inst):
        assert conn.execute("SELECT COUNT(*) AS n FROM charges").fetchone()["n"] == 2
