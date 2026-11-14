"""Response models must not quietly drop a field.

FastAPI filters every response through its `response_model`, so a field the
model forgets disappears from the API without an error anywhere. Two tests in
test_api.py caught that when the models went in -- by accident, because they
happened to read `parts`. This is the check that does not depend on luck: for
each typed endpoint, the model's fields are compared with the keys the domain
function actually returns.
"""

import pytest

from paysleuth.api import schemas
from paysleuth.db import tenant_tx

CASES = []


def case(model, make):
    CASES.append(pytest.param(model, make, id=model.__name__))
    return model


def member_view(conn, inst):
    from paysleuth.members import get_member, list_members
    return get_member(conn, inst, list_members(conn, inst)[0]["id"])


def member_summary(conn, inst):
    from paysleuth.members import list_members
    return list_members(conn, inst)[0]


def credit_detail(conn, inst):
    from paysleuth.payments import get_credit
    with tenant_tx(conn, inst):
        row = conn.execute("SELECT id FROM bank_credits ORDER BY id").fetchone()
    return get_credit(conn, inst, row["id"])


def review_item(conn, inst):
    from datetime import date

    from paysleuth.payments import review_queue
    queue = review_queue(conn, inst, date(2026, 6, 20))
    assert queue, "the demo seed stopped leaving anything in the review queue"
    return queue[0]


def suggestion(conn, inst):
    return review_item(conn, inst)["suggestions"][0]


def staff_row(conn, inst):
    from paysleuth.institutions import list_staff
    return dict(list_staff(conn, inst)[0])


def alert_row(conn, inst):
    """Raised here rather than relied on: the demo seed's close is clean."""
    from paysleuth.alerts import list_alerts, raise_alert
    with tenant_tx(conn, inst):
        raise_alert(conn, inst, "credits_missing", "a line never became a row",
                    "shape-check", missing=[{"utr": "1", "amount": "1.00", "line": 2}])
    return list_alerts(conn, inst, status=None)[0]


def dead_row(conn, inst):
    from paysleuth.outbox import dead_messages
    with tenant_tx(conn, inst):
        conn.execute("UPDATE outbox SET status = 'dead', attempts = 12 "
                     "WHERE id = (SELECT MIN(id) FROM outbox)")
    return dead_messages(conn, inst)[0]


def audit_row(conn, inst):
    from paysleuth import audit
    return audit.recent(conn, inst)[0]


def defaulter_row(conn, inst):
    from datetime import date

    from paysleuth.reports import defaulters
    rows = defaulters(conn, inst, date(2026, 8, 1), min_days=0)
    assert rows, "the demo seed stopped leaving anybody overdue"
    return rows[0]


case(schemas.MemberOut, member_view)
case(schemas.MemberSummary, member_summary)
case(schemas.CreditDetail, credit_detail)
case(schemas.ReviewItem, review_item)
case(schemas.Suggestion, suggestion)
case(schemas.StaffOut, staff_row)
case(schemas.AlertOut, alert_row)
case(schemas.DeadMessageOut, dead_row)
case(schemas.AuditOut, audit_row)
case(schemas.DefaulterOut, defaulter_row)


@pytest.mark.parametrize("model, make", CASES)
def test_a_response_model_describes_every_field_the_domain_returns(model, make, seeded):
    conn, inst = seeded
    real = set(make(conn, inst))
    declared = set(model.model_fields)

    assert not (real - declared), (
        f"{model.__name__} would silently drop {sorted(real - declared)} from the API")
    assert not (declared - real), (
        f"{model.__name__} declares {sorted(declared - real)}, which the domain never returns")


def test_the_untyped_endpoints_are_untyped_on_purpose():
    """The ones left open, so that list is a decision rather than an oversight.

    A daily close and a statement carry a jsonb `figures` whose keys are the
    close's own vocabulary, and the matching report's `signals` block is keyed
    by signal name -- one entry per entry in matching.SIGNALS. Typing those
    would freeze a shape that is meant to grow when a signal is added.
    """
    from paysleuth.api import jobs, reports

    open_by_design = {"/close", "/statements", "/reports/matching",
                      "/reports/ageing", "/reports/collection"}
    typed = set()
    for module in (jobs, reports):
        for route in module.router.routes:
            if route.response_model is not None:
                typed.add(route.path)
    assert not (typed & open_by_design), "something grew a model without this list being updated"


@pytest.fixture
def seeded(pool):
    """A centre mid-month: members, credits, a queue to work, an alert, overdue money."""
    from conftest import DSN
    from tools.seed_demo import main as seed
    seed(["--database-url", f"{DSN}?options=-c%20search_path%3D{pool.schema}"])
    with pool.connection() as conn:
        yield conn, 1
