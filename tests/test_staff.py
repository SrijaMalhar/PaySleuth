"""Staff accounts, who-did-what in the audit log, and two-person approval of large reassignments."""

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import CheckViolation

from conftest import DSN, add_member, credit, fresh_pool, money, together
from paysleuth import audit
from paysleuth.actors import acting_as
from paysleuth.api import create_app
from paysleuth.approvals import decide, list_approvals, request_reassign
from paysleuth.charges import run_billing
from paysleuth.db import tenant_tx
from paysleuth.errors import Conflict, Forbidden
from paysleuth.institutions import actor_for_key, add_staff, create_institution, deactivate_staff
from paysleuth.payments import record_credit, reverse_credit


@pytest.fixture
def centre(conn):
    """An institution with a Rs 5,000 approval limit, an owner, a second owner and two clerks."""
    inst, owner_key = create_institution(conn, "Bright Minds Tuition", approval_threshold=500000,
                                         owner_name="Asha")
    people = {"asha": actor_for_key(conn, owner_key)}
    for name, role in (("Bina", "owner"), ("Chetan", "clerk"), ("Dev", "clerk")):
        people[name.lower()] = actor_for_key(conn, add_staff(conn, inst, name, role)["api_key"])
    diya = add_member(conn, inst, fee="8000")
    kabir = add_member(conn, inst, "Kabir", fee="8000")
    run_billing(conn, inst, date(2026, 6, 1))
    big = record_credit(conn, inst, credit(utr="615412345601", amount="8000",
                                           note=f"PS{1000 + diya}"))["credit"]["id"]
    small = record_credit(conn, inst, credit(utr="615412345602", amount="3000",
                                             note=f"PS{1000 + diya}"))["credit"]["id"]
    return SimpleNamespace(inst=inst, diya=diya, kabir=kabir, big=big, small=small, **people)


def test_a_small_reassignment_happens_at_once(conn, centre):
    result = request_reassign(conn, centre.inst, centre.chetan, centre.small, [(centre.kabir, 300000)])
    assert result["status"] == "applied" and result["changed"] is True
    assert list_approvals(conn, centre.inst) == []


def test_a_large_reassignment_waits_for_a_second_person(conn, centre):
    result = request_reassign(conn, centre.inst, centre.chetan, centre.big, [(centre.kabir, 800000)])
    assert result["status"] == "pending_approval"
    assert money(conn, centre.inst, centre.kabir)["paid"] == 0            # nothing moved yet
    [waiting] = list_approvals(conn, centre.inst)
    assert waiting["requested_by_name"] == "Chetan" and waiting["amount"] == "8000.00"

    done = decide(conn, centre.inst, centre.asha, result["approval_id"], approve=True)
    assert done["status"] == "approved" and done["result"]["changed"] is True
    assert money(conn, centre.inst, centre.kabir)["paid"] == 800000
    assert list_approvals(conn, centre.inst) == []


def test_nobody_can_approve_their_own_request(conn, centre):
    for person in (centre.chetan, centre.asha):                           # not even an owner
        with pytest.raises(Forbidden):
            asked = request_reassign(conn, centre.inst, person, centre.big, [(centre.kabir, 800000)])
            try:
                decide(conn, centre.inst, person, asked["approval_id"], approve=True)
            finally:
                other = centre.bina if person is not centre.bina else centre.asha
                decide(conn, centre.inst, other, asked["approval_id"], approve=False)
    assert money(conn, centre.inst, centre.kabir)["paid"] == 0


def test_one_of_the_two_people_must_be_an_owner(conn, centre):
    asked = request_reassign(conn, centre.inst, centre.chetan, centre.big, [(centre.kabir, 800000)])
    with pytest.raises(Forbidden, match="owner"):
        decide(conn, centre.inst, centre.dev, asked["approval_id"], approve=True)       # clerk + clerk
    decide(conn, centre.inst, centre.bina, asked["approval_id"], approve=True)

    back = request_reassign(conn, centre.inst, centre.asha, centre.big, [(centre.diya, 800000)])
    assert decide(conn, centre.inst, centre.dev, back["approval_id"], approve=True)["status"] == "approved"


def test_the_database_itself_refuses_self_approval(conn, centre):
    asked = request_reassign(conn, centre.inst, centre.chetan, centre.big, [(centre.kabir, 800000)])
    with pytest.raises(CheckViolation):
        with tenant_tx(conn, centre.inst):
            conn.execute("UPDATE approval_requests SET status = 'approved', decided_by = requested_by, "
                         "decided_at = now() WHERE id = %s", (asked["approval_id"],))


def test_a_rejected_request_moves_nothing_and_cannot_be_decided_again(conn, centre):
    asked = request_reassign(conn, centre.inst, centre.chetan, centre.big, [(centre.kabir, 800000)])
    assert decide(conn, centre.inst, centre.asha, asked["approval_id"], approve=False,
                  note="wrong family")["status"] == "rejected"
    with pytest.raises(Conflict):
        decide(conn, centre.inst, centre.bina, asked["approval_id"], approve=True)
    assert money(conn, centre.inst, centre.diya)["paid"] == 1100000


def test_only_one_request_can_wait_per_credit_and_a_bad_one_is_refused_up_front(conn, centre):
    request_reassign(conn, centre.inst, centre.chetan, centre.big, [(centre.kabir, 800000)])
    with pytest.raises(Conflict):
        request_reassign(conn, centre.inst, centre.dev, centre.big, [(centre.kabir, 800000)])
    with pytest.raises(ValueError):
        request_reassign(conn, centre.inst, centre.dev, centre.small, [(centre.kabir, 1)])


def test_two_approvers_at_once_apply_it_once(pool, conn, centre):
    asked = request_reassign(conn, centre.inst, centre.chetan, centre.big, [(centre.kabir, 800000)])
    approvers = [centre.asha, centre.bina]
    results = together(pool, 2, lambda c, i: decide(c, centre.inst, approvers[i], asked["approval_id"],
                                                    approve=True))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, Conflict) for r in results) == 1
    assert money(conn, centre.inst, centre.kabir)["paid"] == 800000


def test_an_approval_is_refused_if_the_credit_changed_in_between(conn, centre):
    asked = request_reassign(conn, centre.inst, centre.chetan, centre.big, [(centre.kabir, 800000)])
    reverse_credit(conn, centre.inst, centre.big)    # the bank returned the credit meanwhile
    with pytest.raises(Conflict):
        decide(conn, centre.inst, centre.asha, asked["approval_id"], approve=True)


def test_the_audit_log_records_who_did_it(conn, centre):
    with acting_as(centre.chetan.staff_id):
        asked = request_reassign(conn, centre.inst, centre.chetan, centre.big, [(centre.kabir, 800000)])
    with acting_as(centre.asha.staff_id):
        decide(conn, centre.inst, centre.asha, asked["approval_id"], approve=True)
    log = [(e["action"], e["actor"]) for e in audit.recent(conn, centre.inst, 3)]
    assert log == [("approval.approved", "Asha"), ("credit.reassigned", "Asha"), ("approval.requested", "Chetan")]
    assert audit.recent(conn, centre.inst, 100)[-1]["actor"] == "system"      # institution.created


def test_staff_keys_and_the_last_owner(conn, centre):
    assert centre.chetan.role == "clerk" and centre.chetan.institution_id == centre.inst
    deactivate_staff(conn, centre.inst, centre.bina.staff_id)
    with pytest.raises(Conflict, match="last active owner"):
        deactivate_staff(conn, centre.inst, centre.asha.staff_id)
    with pytest.raises(ValueError):
        add_staff(conn, centre.inst, "Eve", "admin")


# --- through the API -----------------------------------------------------------------------

def test_roles_and_approval_through_the_api():
    with fresh_pool() as pool:
        app = create_app(DSN, lambda payload: None, today=lambda: date(2026, 6, 20),
                         pool_options=pool.options, pool_size=10)
        with TestClient(app) as client, pool.connection() as conn:
            inst, owner_key = create_institution(conn, "Bright Minds Tuition", approval_threshold=500000,
                                                 owner_name="Asha")
            owner = {"X-API-Key": owner_key}
            made = client.post("/staff", json={"name": "Chetan", "role": "clerk"}, headers=owner)
            assert made.status_code == 201
            clerk = {"X-API-Key": made.json()["api_key"]}
            assert client.get("/me", headers=clerk).json()["role"] == "clerk"
            assert client.post("/staff", json={"name": "X", "role": "clerk"}, headers=clerk).status_code == 403
            assert client.get("/staff", headers=clerk).status_code == 403
            assert "api_key" not in client.get("/staff", headers=owner).json()[0]

            members = [client.post("/members", headers=clerk, json={
                "name": name, "monthly_fee": "8000", "billing_day": 5, "start_period": "2026-06"}).json()
                for name in ("Diya", "Kabir")]
            paid = client.post("/bank-credits", headers=clerk, json={
                "utr": "615412345601", "amount": "8000", "received_at": "2026-06-10T10:00:00+05:30",
                "note": members[0]["ref_code"]}).json()["credit"]
            move = {"parts": [{"member_id": members[1]["id"], "amount": "8000"}]}

            asked = client.post(f"/bank-credits/{paid['id']}/reassign", json=move, headers=clerk)
            assert asked.status_code == 202 and asked.json()["status"] == "pending_approval"
            approval = asked.json()["approval_id"]
            assert client.post(f"/approvals/{approval}/approve", headers=clerk).status_code == 403
            assert client.get("/approvals", headers=owner).json()[0]["requested_by_name"] == "Chetan"
            done = client.post(f"/approvals/{approval}/approve", headers=owner)
            assert done.status_code == 200 and done.json()["status"] == "approved"
            assert client.get(f"/members/{members[1]['id']}", headers=owner).json()["outstanding"] == "0.00"

            log = client.get("/audit", headers=owner).json()
            assert [(e["action"], e["actor"]) for e in log[:3]] == [
                ("approval.approved", "Asha"), ("credit.reassigned", "Asha"), ("approval.requested", "Chetan")]

            # A deactivated key stops working at once.
            staff_id = client.get("/me", headers=clerk).json()["staff_id"]
            client.post(f"/staff/{staff_id}/deactivate", headers=owner)
            assert client.get("/me", headers=clerk).status_code == 401
