"""Every list is bounded, and the bound is one value rather than seven.

The review queue was once read without a bound and the largest institution set
how slow the smallest one's request was. The fix paged it; these tests are what
stops the other lists from each rediscovering the problem.
"""

import pytest

from paysleuth.paging import DEFAULT, MAX, limits


@pytest.mark.parametrize("asked, expect", [
    (None, DEFAULT),      # a caller that forgot gets a page, not the table
    (10, 10),
    (MAX, MAX),
    (MAX + 1, MAX),       # too much becomes as much as you can have, not an error
    (10_000, MAX),
    (0, 1),
    (-5, 1),
])
def test_a_page_size_is_clamped(asked, expect):
    assert limits(asked)[0] == expect


@pytest.mark.parametrize("asked, expect", [(None, 0), (0, 0), (25, 25), (-1, 0)])
def test_an_offset_is_never_negative(asked, expect):
    assert limits(None, asked)[1] == expect


LIST_ENDPOINTS = ["/members", "/statements", "/alerts", "/approvals", "/staff",
                  "/outbox/dead", "/review", "/audit", "/close", "/reports/defaulters"]


def get_route(app, path):
    """The GET route: several paths carry a POST as well, and that one takes a body."""
    return next(r for r in app.routes
                if getattr(r, "path", None) == path and "GET" in getattr(r, "methods", ()))


@pytest.mark.parametrize("path", LIST_ENDPOINTS)
def test_every_list_endpoint_takes_a_page(api, path):
    """The property, asserted per endpoint rather than trusted."""
    params = {p.name for p in get_route(api.client.app, path).dependant.query_params}
    assert "limit" in params, f"{path} returns everything"


@pytest.mark.parametrize("path", LIST_ENDPOINTS)
def test_no_list_endpoint_can_be_asked_for_more_than_the_maximum(api, path):
    assert api.client.get(path, params={"limit": MAX + 1}).status_code == 422
    assert api.client.get(path, params={"limit": 0}).status_code == 422


def test_members_pages_in_order_and_does_not_overlap(api):
    for n in range(7):
        api.client.post("/members", json={
            "name": f"Student {n:02d}", "monthly_fee": "2000.00",
            "billing_day": 5, "start_period": "2026-06"}).raise_for_status()

    first = api.client.get("/members", params={"limit": 3}).json()
    second = api.client.get("/members", params={"limit": 3, "offset": 3}).json()
    rest = api.client.get("/members", params={"limit": 3, "offset": 6}).json()

    assert [len(first), len(second), len(rest)] == [3, 3, 1]
    codes = [m["ref_code"] for m in first + second + rest]
    assert codes == sorted(codes), "pages are not in a stable order"
    assert len(set(codes)) == 7, "a member appeared on two pages"


def test_a_page_of_members_costs_the_same_as_a_page_of_members(api, conn):
    """The balances for a page are read for that page, not for everybody.

    This is the bug the review queue had: the page bound meant nothing because
    the work behind it was still proportional to the whole institution.
    """
    for n in range(12):
        api.client.post("/members", json={
            "name": f"Student {n:02d}", "monthly_fee": "2000.00",
            "billing_day": 5, "start_period": "2026-06"}).raise_for_status()

    from paysleuth.members import list_members
    with count_queries(conn) as counted:
        list_members(conn, api.inst, limit=3)
    small = counted.total
    with count_queries(conn) as counted:
        list_members(conn, api.inst, limit=12)
    assert counted.total == small, "the query count grew with the page size"


class _Counter:
    total = 0


def count_queries(conn):
    """Count executes on a connection for the duration of the block."""
    import contextlib

    @contextlib.contextmanager
    def counting():
        counter = _Counter()
        counter.total = 0
        original = conn.execute

        def execute(*args, **kwargs):
            counter.total += 1
            return original(*args, **kwargs)

        conn.execute = execute
        try:
            yield counter
        finally:
            conn.execute = original

    return counting()
