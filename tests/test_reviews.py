"""Reviews, ratings and abuse reports."""

from __future__ import annotations

import pytest

from app.core.permissions import Role
from app.modules.identity.models import AccountStatus, KycStatus
from app.modules.payments import service as payments
from app.modules.payments.models import TransactionKind
from tests.factories import auth_headers, make_user

API = "/api/v1"


@pytest.fixture
def sale(db):
    buyer = make_user(db, Role.VEHICLE_OWNER)
    mech = make_user(db, Role.MECHANIC, kyc=KycStatus.APPROVED, business_name="Ola Motors")
    tx = payments.create_transaction(
        db, kind=TransactionKind.SERVICE_BOOKING, customer_id=buyer.id,
        provider_id=mech.id, amount_minor=5_000_00,
    )  # fmt: skip
    db.commit()
    return buyer, mech, tx


def test_review_requires_completed_transaction_and_buyer(client, db, sale):
    buyer, mech, tx = sale
    body = {"transaction_ref": tx.ref, "rating": 5, "comment": "Great"}
    assert client.post(f"{API}/reviews", json=body, headers=auth_headers(buyer)).status_code == 422
    payments.mark_completed(db, tx)
    db.commit()
    stranger = make_user(db, Role.VEHICLE_OWNER)
    assert (
        client.post(f"{API}/reviews", json=body, headers=auth_headers(stranger)).status_code == 403
    )
    assert client.post(f"{API}/reviews", json=body, headers=auth_headers(buyer)).status_code == 201
    assert client.post(f"{API}/reviews", json=body, headers=auth_headers(buyer)).status_code == 409
    assert (
        client.post(
            f"{API}/reviews", json={**body, "rating": 6}, headers=auth_headers(buyer)
        ).status_code
        == 422
    )


def test_average_rating_and_reply(client, db, sale):
    buyer, mech, tx = sale
    payments.mark_completed(db, tx)
    other_buyer = make_user(db, Role.VEHICLE_OWNER)
    tx2 = payments.create_transaction(
        db, kind=TransactionKind.SERVICE_BOOKING, customer_id=other_buyer.id,
        provider_id=mech.id, amount_minor=1_000_00,
    )  # fmt: skip
    payments.mark_completed(db, tx2)
    db.commit()
    first = client.post(
        f"{API}/reviews", json={"transaction_ref": tx.ref, "rating": 5}, headers=auth_headers(buyer)
    ).json()
    client.post(
        f"{API}/reviews",
        json={"transaction_ref": tx2.ref, "rating": 4, "comment": "Fine"},
        headers=auth_headers(other_buyer),
    )
    page = client.get(f"{API}/users/{mech.id}/reviews").json()
    assert page["average"] == 4.5 and page["count"] == 2

    reply_path = f"{API}/reviews/{first['id']}/reply"
    assert (
        client.post(reply_path, json={"reply": "Thanks!"}, headers=auth_headers(buyer)).status_code
        == 404
    )
    assert (
        client.post(reply_path, json={"reply": "Thanks!"}, headers=auth_headers(mech)).status_code
        == 200
    )
    assert (
        client.post(reply_path, json={"reply": "Again"}, headers=auth_headers(mech)).status_code
        == 409
    )


def test_reports_dismiss_and_action(client, db, sale):
    buyer, mech, tx = sale
    payments.mark_completed(db, tx)
    db.commit()
    review = client.post(
        f"{API}/reviews", json={"transaction_ref": tx.ref, "rating": 1, "comment": "Abusive text"},
        headers=auth_headers(buyer),
    ).json()  # fmt: skip
    admin = make_user(db, Role.SUPPORT)
    body = {"target_type": "review", "target_id": review["id"], "reason": "abusive_content"}
    assert client.post(f"{API}/reports", json=body, headers=auth_headers(mech)).status_code == 201
    assert client.post(f"{API}/reports", json=body, headers=auth_headers(mech)).status_code == 409
    queue = client.get(f"{API}/admin/reports", headers=auth_headers(admin)).json()
    assert queue["total"] == 1 and queue["items"][0]["ref"].startswith("RPT-")
    resolved = client.post(
        f"{API}/admin/reports/{queue['items'][0]['id']}/resolve",
        json={"action": True},
        headers=auth_headers(admin),
    )
    assert resolved.json()["status"] == "actioned"
    page = client.get(f"{API}/users/{mech.id}/reviews").json()
    assert page["total"] == 0 and page["count"] == 0  # removed review leaves the average


def test_reporting_an_account_suspends_it_on_action(client, db, sale):
    buyer, mech, _ = sale
    admin = make_user(db, Role.ADMIN)
    report = client.post(
        f"{API}/reports",
        json={"target_type": "account", "target_id": str(mech.id), "reason": "fraud_or_scam"},
        headers=auth_headers(buyer),
    ).json()
    client.post(
        f"{API}/admin/reports/{report['id']}/resolve",
        json={"action": True},
        headers=auth_headers(admin),
    )
    db.refresh(mech)
    assert mech.status == AccountStatus.SUSPENDED
    assert (
        client.post(
            f"{API}/admin/reports/{report['id']}/resolve",
            json={"action": False},
            headers=auth_headers(admin),
        ).status_code
        == 422
    )
    nobody = {
        "target_type": "listing",
        "target_id": "00000000-0000-0000-0000-000000000000",
        "reason": "spam",
    }
    assert (
        client.post(f"{API}/reports", json=nobody, headers=auth_headers(buyer)).status_code == 404
    )
