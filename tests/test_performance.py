"""The query count per credit, held to a number, so a loop over members cannot creep back in.

Wall-clock time belongs in bench/, where it is measured and quoted. What a
test can hold steady is the shape: how many round trips one credit costs, and
that the number does not grow with the number of siblings sharing it. On a
database across a network, each round trip costs its latency, so this is the
number that matters.
"""

from contextlib import contextmanager
from datetime import date

import pytest

from conftest import add_member, credit
from paysleuth.charges import run_billing
from paysleuth.payments import record_credit, reverse_credit, review_queue

JUNE = date(2026, 6, 1)


@contextmanager
def counting(conn):
    """Count execute() calls on this connection."""
    real = conn.execute
    counted = []
    conn.execute = lambda sql, params=None, **kw: (counted.append(sql), real(sql, params, **kw))[1]
    try:
        yield counted
    finally:
        conn.execute = real


def _family(conn, inst, children, *, billed=True):
    ids = [add_member(conn, inst, f"Child {i}", fee=str(1000 + 100 * i), upi_ids=["parent@oksbi"])
           for i in range(children)]
    if billed:
        run_billing(conn, inst, JUNE)
    return ids, sum(100000 + 10000 * i for i in range(children))


@pytest.mark.parametrize("children", [1, 2, 4, 8])
def test_a_credit_for_one_child_costs_the_same_however_large_the_family(conn, inst, children):
    _family(conn, inst, children)
    with counting(conn) as queries:
        result = record_credit(conn, inst, credit(amount=str(1000 + 100 * (children - 1)),
                                                  note=f"PS{1000 + 1}"))
    assert result["credit"]["status"] == "matched"
    assert len(queries) == 11, "\n".join(" ".join(str(q).split())[:70] for q in queries)


def test_a_sibling_split_does_not_cost_a_query_per_child(conn, inst):
    ids, total = _family(conn, inst, 6)
    with counting(conn) as queries:
        result = record_credit(conn, inst, credit(amount=str(total // 100), upi="parent@oksbi"))
    assert result["credit"]["match_rule"] == "sibling_split" and len(result["applied"]) == len(ids)
    assert len(queries) == 12, "\n".join(" ".join(str(q).split())[:70] for q in queries)


def test_an_unmatched_credit_costs_four_queries(conn, inst):
    add_member(conn, inst)
    with counting(conn) as queries:
        record_credit(conn, inst, credit(upi="stranger@ybl"))
    assert len(queries) == 4


def test_a_replayed_alert_is_cheap(conn, inst):
    diya = add_member(conn, inst)
    run_billing(conn, inst, JUNE)
    record_credit(conn, inst, credit(note=f"PS{1000 + diya}"))
    with counting(conn) as queries:
        assert record_credit(conn, inst, credit(note=f"PS{1000 + diya}"))["duplicate"] is True
    # The cheap pre-check answers it: no member locks, no decision, no insert.
    assert len(queries) == 2


def test_reversing_a_sibling_split_does_not_cost_a_query_per_child(conn, inst):
    ids, total = _family(conn, inst, 6)
    credit_id = record_credit(conn, inst, credit(amount=str(total // 100),
                                                 upi="parent@oksbi"))["credit"]["id"]
    with counting(conn) as queries:
        assert len(reverse_credit(conn, inst, credit_id)["reopened"]) == len(ids)
    assert len(queries) == 13, "\n".join(" ".join(str(q).split())[:70] for q in queries)


def test_the_review_queue_reads_the_members_once_not_once_per_credit(conn, inst):
    for i in range(20):
        add_member(conn, inst, f"Child {i}", upi_ids=[f"parent{i}@oksbi"])
    run_billing(conn, inst, JUNE)
    for i in range(25):
        record_credit(conn, inst, credit(utr=f"6154000000{i:02d}", amount="777", upi=f"walkin{i}@ybl",
                                         name="SOMEONE ELSE", note="fees"))
    with counting(conn) as queries:
        queue = review_queue(conn, inst, date(2026, 6, 20))
    assert len(queue) == 25 and all(q["suggestions"] is not None for q in queue)
    # set_config, the credits, then the members, dues, fees and families once each.
    assert len(queries) == 6, "\n".join(" ".join(str(q).split())[:70] for q in queries)
