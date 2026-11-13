from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import DSN, fresh_pool
from paysleuth.api import create_app
from paysleuth.institutions import create_institution

TODAY = date(2026, 6, 20)
DAYTIME = datetime(2030, 1, 15, 5, 0, tzinfo=timezone.utc)       # 10:30 IST, after the real clock

STATEMENT = """Txn Date,Narration,Chq/Ref No,Withdrawal Amt,Deposit Amt,Balance
06/06/2026,UPI/CR/615412345601/SUNITA SHARMA/SBIN/sunita.sharma@oksbi/June fees,615412345601,,"2,000.00",
07/06/2026,UPI-R K VERMA-rk.verma@okhdfcbank-HDFC0001234-615412345602-JUNE FEES,615412345602,,"3,500.00",
08/06/2026,UPI/CR/615412345604/LAKSHMI IYER/UBIN/lakshmi.iyer@oksbi/meera fees,615412345604,,"2,500.00",
08/06/2026,UPI/CR/615412345604/LAKSHMI IYER/UBIN/lakshmi.iyer@oksbi/meera fees,615412345604,,"2,500.00",
09/06/2026,ATM WDL,,500.00,,
"""


@pytest.fixture
def api():
    with fresh_pool() as pool:
        sent = []
        app = create_app(DSN, sent.append, today=lambda: TODAY, now=lambda: DAYTIME,
                         pool_options=pool.options, pool_size=10)
        with TestClient(app) as client, pool.connection() as conn:
            inst, key = create_institution(conn, "Bright Minds Tuition")
            other, other_key = create_institution(conn, "Other Centre")
            client.headers["X-API-Key"] = key
            yield SimpleNamespace(client=client, sent=sent, inst=inst, other_key=other_key)


def _member(api, name="Diya Sharma", fee="2000", **extra):
    body = {"name": name, "monthly_fee": fee, "billing_day": 5, "start_period": "2026-06", **extra}
    response = api.client.post("/members", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_health(api):
    assert api.client.get("/health").json() == {"status": "ok"}


def test_requests_need_a_valid_key(api):
    assert api.client.get("/members", headers={"X-API-Key": ""}).status_code == 401
    assert api.client.get("/members", headers={"X-API-Key": "ps_wrong"}).status_code == 401
    assert api.client.get("/members").status_code == 200


def test_member_edits_need_the_current_etag(api):
    created = api.client.post("/members", json={"name": "Diya", "monthly_fee": "2000", "billing_day": 5,
                                                "start_period": "2026-06"})
    assert created.headers["ETag"] == '"1"'
    url = f"/members/{created.json()['id']}"
    assert api.client.patch(url, json={"phone": "9876543210"}).status_code == 428
    ok = api.client.patch(url, json={"phone": "9876543210"}, headers={"If-Match": '"1"'})
    assert ok.status_code == 200 and ok.headers["ETag"] == '"2"' and ok.json()["phone"] == "9876543210"
    stale = api.client.patch(url, json={"phone": "9123456789"}, headers={"If-Match": '"1"'})
    assert stale.status_code == 412
    assert api.client.patch(url, json={}, headers={"If-Match": "1"}).status_code == 400
    assert api.client.get(url).headers["ETag"] == '"2"'


def test_validation_errors_are_422(api):
    base = {"name": "Diya", "monthly_fee": "2000", "billing_day": 5, "start_period": "2026-06"}
    for bad in [{"monthly_fee": "12.345"}, {"monthly_fee": 2000.5}, {"billing_day": 40},
                {"start_period": "June"}, {"phone": "12345"}, {"upi_ids": ["not-a-upi"]}, {"name": "  "}]:
        assert api.client.post("/members", json={**base, **bad}).status_code == 422, bad
    assert api.client.post("/members", json={"name": "Diya"}).status_code == 422


def test_a_credit_from_a_bank_alert_narration_is_parsed_and_matched(api):
    diya = _member(api)
    api.client.post("/billing/run")
    body = {"narration": f"UPI/CR/615412345601/SUNITA SHARMA/SBIN/sunita@oksbi/fees {diya['ref_code']}",
            "amount": "2000", "received_at": "2026-06-10T10:00:00"}
    first = api.client.post("/bank-credits", json=body)
    assert first.status_code == 201
    assert first.json()["credit"]["match_rule"] == "ref_code"
    assert first.json()["credit"]["received_at"] == "2026-06-10T10:00:00+05:30"
    again = api.client.post("/bank-credits", json=body)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert api.client.post("/bank-credits", json={**body, "amount": "2500"}).status_code == 409
    assert api.client.post("/bank-credits", json={**body, "narration": "CASH"}).status_code == 422


def test_statement_import_review_queue_and_corrections_end_to_end(api):
    diya = _member(api, upi_ids=["sunita.sharma@oksbi"])
    rohan = _member(api, "Rohan Verma", fee="2000", guardian_name="Rajesh Kumar Verma",
                    upi_ids=["rk.verma@okhdfcbank"])
    kabir = _member(api, "Kabir Verma", fee="1500", guardian_name="Rajesh Kumar Verma",
                    upi_ids=["rk.verma@okhdfcbank"])
    meera = _member(api, "Meera Iyer", fee="2500", guardian_name="Lakshmi Iyer")
    assert api.client.post("/billing/run").json()["charges_created"] == 4

    imported = api.client.post("/bank-credits/import",
                               files={"file": ("june.csv", "﻿" + STATEMENT, "text/csv")}).json()
    assert (imported["recorded"], imported["matched"], imported["needs_review"],
            imported["duplicates"], imported["skipped_debits"]) == (3, 2, 1, 1, 1)

    [waiting] = api.client.get("/review").json()
    assert waiting["utr"] == "615412345604"
    top = waiting["suggestions"][0]
    assert top["member_id"] == meera["id"] and "note mentions Meera" in top["reasons"]

    assigned = api.client.post(f"/review/{waiting['id']}/assign", json={"member_id": top["member_id"]})
    assert assigned.status_code == 200 and assigned.json()["credit"]["match_rule"] == "manual"
    assert api.client.get("/review").json() == []

    # The clerk got it wrong: move it. Then move it back.
    moved = api.client.post(f"/bank-credits/{waiting['id']}/reassign",
                            json={"parts": [{"member_id": diya["id"], "amount": "2500.00"}]})
    assert moved.status_code == 200
    assert api.client.get(f"/members/{meera['id']}").json()["outstanding"] == "2500.00"
    api.client.post(f"/bank-credits/{waiting['id']}/reassign",
                    json={"parts": [{"member_id": meera["id"], "amount": "2500.00"}]})
    for m in (diya, rohan, kabir, meera):
        assert api.client.get(f"/members/{m['id']}").json()["outstanding"] == "0.00"
    # The learned UPI ID pays July without help.
    july = api.client.post("/bank-credits", json={
        "narration": "UPI/CR/615412349999/LAKSHMI IYER/UBIN/lakshmi.iyer@oksbi/july",
        "amount": "2500", "received_at": "2026-07-06T09:00:00+05:30"}).json()
    assert july["credit"]["match_rule"] == "known_upi_id"

    split = api.client.post("/bank-credits", json={
        "utr": "615412340001", "amount": "100", "received_at": "2026-06-12T09:00:00+05:30",
        "payer_upi_id": "walkin@ybl"}).json()
    bad = api.client.post(f"/review/{split['credit']['id']}/split",
                          json={"parts": [{"member_id": diya["id"], "amount": "60"}]})
    assert bad.status_code == 422
    good = api.client.post(f"/review/{split['credit']['id']}/split",
                           json={"parts": [{"member_id": diya["id"], "amount": "60"},
                                           {"member_id": rohan["id"], "amount": "40"}]})
    assert good.status_code == 200 and len(good.json()["applied"]) == 2
    assert api.client.get(f"/bank-credits/{split['credit']['id']}").json()["parts"][0]["amount"] == "60.00"


def test_an_upload_over_10_mb_is_refused(api):
    big = "x" * (10 * 1024 * 1024 + 1)
    assert api.client.post("/bank-credits/import", files={"file": ("big.csv", big)}).status_code == 413
    assert api.client.post("/bank-credits/import",
                           files={"file": ("bad.csv", b"\xff\xfe\x00")}).status_code == 422


def test_collection_ageing_and_defaulters_reports(api):
    diya = _member(api, start_period="2026-04")
    _member(api, "Kabir", fee="1500")
    api.client.post("/billing/run")                               # Diya: April, May, June; Kabir: June
    api.client.post("/bank-credits", json={"utr": "615412345601", "amount": "1500",
                                           "received_at": "2026-06-10T10:00:00+05:30",
                                           "note": diya["ref_code"]})
    june = api.client.get("/reports/collection", params={"period": "2026-06"}).json()
    assert (june["billed"], june["collected"], june["collection_rate_pct"], june["paid_in_full"]) == \
        ("3500.00", "0.00", "0.0", 0)
    april = api.client.get("/reports/collection", params={"period": "2026-04"}).json()
    assert april["collected"] == "1500.00" and april["collection_rate_pct"] == "75.0"

    ages = api.client.get("/reports/ageing").json()
    assert ages["totals"] == {"not_yet_due": "0.00", "days_1_30": "3500.00", "days_31_60": "2000.00",
                              "days_61_90": "500.00", "over_90": "0.00", "total": "6000.00"}
    defaulters = api.client.get("/reports/defaulters", params={"min_days": 30}).json()
    assert [(d["member_id"], d["overdue"], d["oldest_days_overdue"]) for d in defaulters] == \
        [(diya["id"], "2500.00", 76)]
    assert api.client.get("/audit").json()[0]["action"] in ("credit.assigned", "member.billed")


def test_receipts_and_reminders_are_delivered_through_the_relay(api):
    diya = _member(api, phone="9876543210")
    kabir = _member(api, "Kabir", fee="1500")
    api.client.post("/billing/run")
    api.client.post("/bank-credits", json={"utr": "615412345601", "amount": "2000",
                                           "received_at": "2026-06-10T10:00:00+05:30",
                                           "note": diya["ref_code"]})
    assert api.client.post("/reminders/run").json() == {"queued": 1}       # Kabir is 15 days overdue
    assert api.client.post("/outbox/relay").json()["sent"] == 2
    kinds = sorted((m["kind"], m["member_id"]) for m in api.sent)
    assert kinds == [("overdue_reminder", kabir["id"]), ("payment_receipt", diya["id"])]
    assert api.client.get("/outbox/dead").json() == []


def test_tenants_cannot_see_each_other(api):
    diya = _member(api)
    other = {"X-API-Key": api.other_key}
    assert api.client.get(f"/members/{diya['id']}", headers=other).status_code == 404
    assert api.client.get("/members", headers=other).json() == []
    assert api.client.get(f"/members/{diya['id']}/statement", headers=other).status_code == 404


def test_a_concession_shows_on_the_member_statement(api):
    asha = _member(api, "Asha", fee="2000", concession_pct=25)
    api.client.post("/billing/run")
    lines = api.client.get(f"/members/{asha['id']}/statement").json()
    assert lines["charges"] == [{"period": "2026-06", "kind": "monthly_fee", "gross": "2000.00",
                                 "concession": "500.00", "amount": "1500.00", "paid": "0.00",
                                 "status": "unpaid", "due_date": "2026-06-05", "days_overdue": 15}]
    assert lines["totals"]["outstanding"] == "1500.00"


def test_deactivating_needs_the_etag_and_stops_billing(api):
    diya = _member(api)
    url = f"/members/{diya['id']}/deactivate"
    assert api.client.post(url).status_code == 428
    done = api.client.post(url, headers={"If-Match": '"1"'})
    assert done.status_code == 200 and done.json()["active"] is False
    assert api.client.post("/billing/run").json()["charges_created"] == 0
