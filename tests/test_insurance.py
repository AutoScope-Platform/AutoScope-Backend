"""Insurance: application, offer, payment, policy issue, claims, renewal and the daily job."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.permissions import Role
from app.modules.identity.models import KycStatus
from app.modules.insurance import service
from app.modules.insurance.models import Policy, PolicyStatus
from tests.factories import auth_headers, make_user

API = "/api/v1"


@pytest.fixture
def world(client, db):
    admin = make_user(db, Role.ADMIN)
    insurer = make_user(db, Role.INSURER, kyc=KycStatus.APPROVED, business_name="SafeCover")
    customer = make_user(db, Role.VEHICLE_OWNER)
    product = client.post(
        f"{API}/listings",
        json={"type": "insurance_product", "title": "Comprehensive Motor", "price_minor": 0,
              "attributes": {"cover_type": "comprehensive"}},
        headers=auth_headers(insurer),
    ).json()  # fmt: skip
    client.post(
        f"{API}/admin/listings/{product['id']}/decision",
        json={"approve": True},
        headers=auth_headers(admin),
    )
    vehicle = client.post(
        f"{API}/me/vehicles", json={"make": "Toyota", "model": "Camry", "year": 2018, "vin": "1HGCM82633A004352"},
        headers=auth_headers(customer),
    ).json()  # fmt: skip
    return insurer, customer, product, vehicle


def _apply(client, customer, product, vehicle):
    response = client.post(
        f"{API}/insurance/applications",
        json={"product_id": product["id"], "vehicle_id": vehicle["id"], "details": "Private use"},
        headers=auth_headers(customer),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _buy(client, insurer, customer, application, premium=185_000_00):
    path = f"{API}/insurance/applications/{application['id']}"
    offer = client.put(
        f"{path}/offer",
        json={"premium_minor": premium, "terms": "Excess N50,000"},
        headers=auth_headers(insurer),
    )
    assert offer.status_code == 200, offer.text
    accepted = client.post(
        f"{path}/answer", json={"accept": True}, headers=auth_headers(customer)
    ).json()
    paid = client.post(
        f"{API}/payments",
        json={"transaction_id": accepted["transaction_id"]},
        headers={**auth_headers(customer), "Idempotency-Key": f"p-{application['id']}"},
    ).json()
    client.post(f"{API}/payments/{paid['reference']}/verify", headers=auth_headers(customer))
    return path


def test_application_to_active_policy(client, db, world):
    insurer, customer, product, vehicle = world
    application = _apply(client, customer, product, vehicle)
    assert application["status"] == "awaiting_insurer" and application["ref"].startswith("POL-")
    assert application["vehicle_summary"] == "2018 Toyota Camry"
    path = f"{API}/insurance/applications/{application['id']}"
    assert (
        client.post(
            f"{path}/answer", json={"accept": True}, headers=auth_headers(customer)
        ).status_code
        == 422
    )  # no offer
    assert (
        client.put(
            f"{path}/offer",
            json={"premium_minor": 1, "terms": "x y z"},
            headers=auth_headers(customer),
        ).status_code
        == 403
    )
    _buy(client, insurer, customer, application)
    final = client.get(path, headers=auth_headers(customer)).json()
    assert final["status"] == "active" and final["escrow_status"] == "held"
    policies = client.get(f"{API}/me/policies", headers=auth_headers(customer)).json()
    assert len(policies) == 1 and policies[0]["policy_number"].startswith("AS-POL-")
    assert policies[0]["ends_on"] == str(date.today() + timedelta(days=365))
    assert (
        len(client.get(f"{API}/me/policies?as_insurer=true", headers=auth_headers(insurer)).json())
        == 1
    )
    cert = client.get(
        f"{API}/insurance/policies/{policies[0]['id']}/certificate", headers=auth_headers(customer)
    )
    assert (
        cert.status_code == 200
        and "Certificate of Insurance" in cert.text
        and "SafeCover" in cert.text
    )
    stranger = make_user(db, Role.VEHICLE_OWNER)
    assert (
        client.get(
            f"{API}/insurance/policies/{policies[0]['id']}", headers=auth_headers(stranger)
        ).status_code
        == 404
    )


def test_decline_and_withdraw(client, db, world):
    insurer, customer, product, vehicle = world
    first = _apply(client, customer, product, vehicle)
    declined = client.post(
        f"{API}/insurance/applications/{first['id']}/decline",
        json={"reason": "Vehicle too old"},
        headers=auth_headers(insurer),
    ).json()
    assert declined["status"] == "declined" and declined["decline_reason"] == "Vehicle too old"
    second = _apply(client, customer, product, vehicle)
    path = f"{API}/insurance/applications/{second['id']}"
    client.put(
        f"{path}/offer",
        json={"premium_minor": 100_000_00, "terms": "Standard"},
        headers=auth_headers(insurer),
    )
    client.post(f"{path}/answer", json={"accept": True}, headers=auth_headers(customer))
    assert (
        client.post(f"{path}/cancel", headers=auth_headers(customer)).json()["status"]
        == "cancelled"
    )


def test_expired_offer_cannot_be_accepted(client, db, world):
    insurer, customer, product, vehicle = world
    application = _apply(client, customer, product, vehicle)
    path = f"{API}/insurance/applications/{application['id']}"
    client.put(
        f"{path}/offer",
        json={"premium_minor": 100_000_00, "terms": "Standard", "valid_days": 1},
        headers=auth_headers(insurer),
    )
    from app.core.db import utcnow
    from app.modules.insurance.models import Application

    row = db.get(Application, application["id"])
    row.offer_valid_until = utcnow() - timedelta(hours=1)
    db.commit()
    assert (
        client.post(
            f"{path}/answer", json={"accept": True}, headers=auth_headers(customer)
        ).status_code
        == 422
    )


def test_claims_flow(client, db, world):
    insurer, customer, product, vehicle = world
    application = _apply(client, customer, product, vehicle)
    _buy(client, insurer, customer, application)
    policy = client.get(f"{API}/me/policies", headers=auth_headers(customer)).json()[0]
    claims = f"{API}/insurance/policies/{policy['id']}/claims"
    body = {
        "incident_date": str(date.today()),
        "description": "Rear-ended at a junction",
        "amount_claimed_minor": 400_000_00,
    }
    assert (
        client.post(
            claims,
            json={**body, "incident_date": str(date.today() - timedelta(days=30))},
            headers=auth_headers(customer),
        ).status_code
        == 422
    )
    assert (
        client.post(
            claims, json=body, headers=auth_headers(make_user(db, Role.VEHICLE_OWNER))
        ).status_code
        == 404
    )
    claim = client.post(claims, json=body, headers=auth_headers(customer)).json()
    assert claim["status"] == "submitted" and claim["ref"].startswith("CLM-")
    doc = client.post(
        f"{API}/insurance/claims/{claim['id']}/documents",
        files={"file": ("damage.jpg", b"\xff\xd8\xff" + b"x" * 50, "image/jpeg")},
        headers=auth_headers(customer),
    )
    assert doc.status_code == 201
    bad = client.post(
        f"{API}/insurance/claims/{claim['id']}/documents",
        files={"file": ("x.exe", b"MZ", "application/octet-stream")},
        headers=auth_headers(customer),
    )
    assert bad.status_code == 400
    path = f"{API}/insurance/claims/{claim['id']}/decision"
    assert (
        client.post(path, json={"status": "approved"}, headers=auth_headers(customer)).status_code
        == 403
    )
    assert (
        client.post(path, json={"status": "approved"}, headers=auth_headers(insurer)).status_code
        == 400
    )  # needs amount
    assert (
        client.post(
            path,
            json={"status": "approved", "approved_amount_minor": 500_000_00},
            headers=auth_headers(insurer),
        ).status_code
        == 400
    )
    client.post(path, json={"status": "under_review"}, headers=auth_headers(insurer))
    approved = client.post(
        path,
        json={
            "status": "approved",
            "approved_amount_minor": 350_000_00,
            "note": "Repair quote agreed",
        },
        headers=auth_headers(insurer),
    ).json()
    assert (
        approved["status"] == "approved"
        and approved["decided_at"]
        and len(approved["documents"]) == 1
    )
    assert (
        client.post(path, json={"status": "rejected"}, headers=auth_headers(insurer)).status_code
        == 422
    )
    mine = client.get(f"{API}/insurance/claims", headers=auth_headers(customer)).json()
    assert mine["total"] == 1
    assert (
        client.get(f"{API}/insurance/claims?as_insurer=true", headers=auth_headers(insurer)).json()[
            "total"
        ]
        == 1
    )


def test_renewal_continues_from_the_end_date_and_job_reminds(client, db, world):
    insurer, customer, product, vehicle = world
    first = _apply(client, customer, product, vehicle)
    _buy(client, insurer, customer, first)
    policy = client.get(f"{API}/me/policies", headers=auth_headers(customer)).json()[0]
    renewal = client.post(
        f"{API}/insurance/policies/{policy['id']}/renew", headers=auth_headers(customer)
    ).json()
    assert renewal["is_renewal"] is True and renewal["status"] == "awaiting_insurer"
    _buy(client, insurer, customer, renewal, premium=190_000_00)
    policies = client.get(f"{API}/me/policies", headers=auth_headers(customer)).json()
    starts = sorted(p["starts_on"] for p in policies)
    assert starts[1] == policy["ends_on"]  # no gap in cover

    # Daily job: remind 30 days before the end, once, and expire after it.
    soon = date.fromisoformat(policy["ends_on"]) - timedelta(days=10)
    assert service.run_policy_lifecycle(db, today=soon) == {"expired": 0, "reminded": 1}
    assert service.run_policy_lifecycle(db, today=soon) == {"expired": 0, "reminded": 0}
    after = date.fromisoformat(policy["ends_on"]) + timedelta(days=1)
    assert service.run_policy_lifecycle(db, today=after)["expired"] == 1
    assert db.get(Policy, policy["id"]).status == PolicyStatus.EXPIRED
