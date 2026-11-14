"""The client's own retry key.

The domain keys cover repeated operations that have a natural identity. This
covers the one that does not: a client that timed out cannot tell a lost
request from a lost response, and two students called Diya Sharma are two
students.
"""

from datetime import timedelta

import pytest

from paysleuth.db import tenant_tx
from paysleuth.idempotency import TAKEOVER_AFTER, forget_old

MEMBER = {"name": "Diya Sharma", "monthly_fee": "2000.00", "billing_day": 5,
          "start_period": "2026-06"}


def members(api):
    return api.client.get("/members", params={"limit": 200}).json()


def test_without_a_key_a_repeated_post_creates_two_members(api):
    """The thing the header exists to prevent, demonstrated first."""
    api.client.post("/members", json=MEMBER).raise_for_status()
    api.client.post("/members", json=MEMBER).raise_for_status()
    assert len(members(api)) == 2


def test_a_retry_under_the_same_key_returns_the_first_reply(api):
    first = api.client.post("/members", json=MEMBER, headers={"Idempotency-Key": "abc-123"})
    second = api.client.post("/members", json=MEMBER, headers={"Idempotency-Key": "abc-123"})

    assert first.status_code == second.status_code
    assert first.json() == second.json(), "the retry got a different member"
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert first.headers.get("Idempotency-Replayed") is None
    assert len(members(api)) == 1


def test_different_keys_are_different_requests(api):
    api.client.post("/members", json=MEMBER, headers={"Idempotency-Key": "one"}).raise_for_status()
    api.client.post("/members", json=MEMBER, headers={"Idempotency-Key": "two"}).raise_for_status()
    assert len(members(api)) == 2


def test_the_same_key_with_a_different_body_is_refused(api):
    api.client.post("/members", json=MEMBER, headers={"Idempotency-Key": "k"}).raise_for_status()
    other = api.client.post("/members", json={**MEMBER, "name": "Rohan Verma"},
                            headers={"Idempotency-Key": "k"})
    assert other.status_code == 412
    assert "different request" in other.json()["detail"]
    assert len(members(api)) == 1, "the second body was created anyway"


def test_the_same_key_on_a_different_path_is_refused(api):
    api.client.post("/members", json=MEMBER, headers={"Idempotency-Key": "k"}).raise_for_status()
    elsewhere = api.client.post("/billing/run", json={"period": "2026-06"},
                                headers={"Idempotency-Key": "k"})
    assert elsewhere.status_code == 412


def test_a_failed_request_may_be_retried_under_the_same_key(api):
    """A rejected body is the client's to fix, not a result to replay forever."""
    bad = api.client.post("/members", json={**MEMBER, "billing_day": 99},
                          headers={"Idempotency-Key": "retry-me"})
    assert bad.status_code == 422

    good = api.client.post("/members", json=MEMBER, headers={"Idempotency-Key": "retry-me"})
    assert good.status_code == 201, good.json()
    assert len(members(api)) == 1


def _stall(api, key, age=None):
    """Make a finished key look like a request that is still running."""
    with tenant_tx(api.conn, api.inst):
        api.conn.execute(
            "UPDATE idempotency_keys SET finished_at = NULL, response = NULL, status_code = NULL, "
            "reserved_at = now() - %s WHERE key = %s", (age or timedelta(0), key))


def test_a_request_still_running_makes_the_retry_wait(api):
    """Reserved but unfinished: the first request has not replied yet."""
    api.client.post("/members", json=MEMBER,
                    headers={"Idempotency-Key": "inflight"}).raise_for_status()
    _stall(api, "inflight")

    busy = api.client.post("/members", json=MEMBER, headers={"Idempotency-Key": "inflight"})
    assert busy.status_code == 409
    assert "still running" in busy.json()["detail"]
    assert len(members(api)) == 1


def test_a_different_request_under_an_in_flight_key_is_refused_not_queued(api):
    """Which error wins when a key is both in flight and reused wrongly."""
    api.client.post("/members", json=MEMBER,
                    headers={"Idempotency-Key": "mixed"}).raise_for_status()
    _stall(api, "mixed")

    wrong = api.client.post("/members", json={**MEMBER, "name": "Rohan Verma"},
                            headers={"Idempotency-Key": "mixed"})
    assert wrong.status_code == 412, "a client bug was reported as a race"


def test_a_reservation_nobody_finished_is_taken_over(api):
    """A process that died holding a key must not wedge it forever.

    And the cost of that, stated: the work can run a second time. The transport
    key cannot prevent it, because the handler's transaction committed before
    the key record did. This is exactly why the domain keys exist -- the same
    sequence against POST /bank-credits is harmless, because the bank's UTR is
    unique. For /members, which has no natural identity, two rows is the honest
    outcome and a 60-second window is the bound on how often it can happen.
    """
    api.client.post("/members", json=MEMBER,
                    headers={"Idempotency-Key": "abandoned"}).raise_for_status()
    _stall(api, "abandoned", TAKEOVER_AFTER + timedelta(seconds=5))

    taken = api.client.post("/members", json=MEMBER, headers={"Idempotency-Key": "abandoned"})
    assert taken.status_code == 201, taken.json()
    assert len(members(api)) == 2, "a takeover that did not re-run is a key that stayed wedged"


def test_one_institution_cannot_see_anothers_keys(api):
    api.client.post("/members", json=MEMBER, headers={"Idempotency-Key": "shared"}).raise_for_status()
    theirs = api.client.post("/members", json=MEMBER,
                             headers={"Idempotency-Key": "shared", "X-API-Key": api.other_key})
    assert theirs.status_code == 201
    assert theirs.headers.get("Idempotency-Replayed") is None, "replayed another tenant's reply"


def test_a_key_longer_than_the_limit_is_refused(api):
    long = api.client.post("/members", json=MEMBER, headers={"Idempotency-Key": "x" * 300})
    assert long.status_code == 412
    assert "at most" in long.json()["detail"]


def test_a_get_ignores_the_header(api):
    """Reads are already safe; a key on one would only grow the table."""
    assert api.client.get("/members", headers={"Idempotency-Key": "pointless"}).status_code == 200
    with_key = api.client.get("/members", headers={"Idempotency-Key": "pointless"})
    assert with_key.headers.get("Idempotency-Replayed") is None


def test_old_keys_are_forgotten(api):
    api.client.post("/members", json=MEMBER, headers={"Idempotency-Key": "old"}).raise_for_status()
    with tenant_tx(api.conn, api.inst):
        api.conn.execute("UPDATE idempotency_keys SET reserved_at = now() - interval '8 days'")
    assert forget_old(api.conn, api.inst) == 1
    with tenant_tx(api.conn, api.inst):
        assert api.conn.execute("SELECT COUNT(*) AS n FROM idempotency_keys").fetchone()["n"] == 0


@pytest.mark.parametrize("path, payload", [
    ("/billing/run", {"period": "2026-06"}),
    ("/reminders/run", {}),
])
def test_other_mutating_endpoints_honour_the_key(api, path, payload):
    first = api.client.post(path, json=payload, headers={"Idempotency-Key": f"k{path}"})
    second = api.client.post(path, json=payload, headers={"Idempotency-Key": f"k{path}"})
    assert first.status_code == second.status_code
    assert second.headers.get("Idempotency-Replayed") == "true"
