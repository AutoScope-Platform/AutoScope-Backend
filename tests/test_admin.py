"""Admin console: overview, verification queue, users, audit log, provider analytics."""

from __future__ import annotations

from app.core.permissions import Role
from app.modules.identity.models import KycStatus
from app.modules.payments import service as payments
from app.modules.payments.models import TransactionKind
from tests.factories import auth_headers, make_user

API = "/api/v1"


def _paid_sale(db, amount=10_000_00):
    buyer = make_user(db, Role.VEHICLE_OWNER)
    mech = make_user(db, Role.MECHANIC, kyc=KycStatus.APPROVED, business_name="Ola Motors")
    tx = payments.create_transaction(
        db, kind=TransactionKind.SERVICE_BOOKING, customer_id=buyer.id,
        provider_id=mech.id, amount_minor=amount,
    )  # fmt: skip
    db.commit()
    return buyer, mech, tx


def test_admin_endpoints_are_staff_only(client, db):
    owner = make_user(db, Role.VEHICLE_OWNER)
    for path in ("/admin/overview", "/admin/users", "/admin/kyc", "/admin/audit"):
        assert client.get(f"{API}{path}", headers=auth_headers(owner)).status_code == 403
        assert client.get(f"{API}{path}").status_code == 401


def test_overview_has_expected_shape(client, db):
    support = make_user(db, Role.SUPPORT)
    make_user(db, Role.VEHICLE_OWNER)
    r = client.get(f"{API}/admin/overview", headers=auth_headers(support))
    assert r.status_code == 200
    body = r.json()
    assert body["users"]["total"] >= 2
    assert set(body["queues"]) == {
        "kyc_pending",
        "listings_pending",
        "disputes_open",
        "reports_open",
    }
    assert body["gmv_by_category"]


def test_users_search_suspend_and_audit(client, db):
    admin = make_user(db, Role.ADMIN)
    target = make_user(db, Role.VEHICLE_OWNER, name="Zed Unique Person")
    h = auth_headers(admin)
    found = client.get(f"{API}/admin/users", params={"q": "Unique"}, headers=h).json()
    assert [u["id"] for u in found["items"]] == [str(target.id)]
    assert (
        client.post(
            f"{API}/admin/users/{target.id}/suspend", json={"reason": "fraud"}, headers=h
        ).json()["status"]
        == "suspended"
    )
    assert (
        client.post(f"{API}/admin/users/{target.id}/reactivate", json={}, headers=h).json()[
            "status"
        ]
        == "active"
    )
    log = client.get(f"{API}/admin/audit", params={"category": "admin"}, headers=h).json()
    assert log["total"] >= 2


def test_audit_export_is_super_admin_only_and_logged(client, db):
    support = make_user(db, Role.SUPPORT)
    admin = make_user(db, Role.ADMIN)
    assert client.get(f"{API}/admin/audit/export", headers=auth_headers(support)).status_code == 403
    r = client.get(f"{API}/admin/audit/export", headers=auth_headers(admin))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("time_utc,category")
    log = client.get(
        f"{API}/admin/audit", params={"q": "Exported"}, headers=auth_headers(admin)
    ).json()
    assert log["total"] == 1


def test_csv_cells_cannot_run_as_formulas():
    from app.modules.admin.service import _csv_safe

    assert _csv_safe("=SUM(A1)") == "'=SUM(A1)"
    assert _csv_safe("Adaora") == "Adaora"


def test_grant_staff_role_super_admin_only(client, db):
    support = make_user(db, Role.SUPPORT)
    admin = make_user(db, Role.ADMIN)
    person = make_user(db, Role.VEHICLE_OWNER)
    url = f"{API}/admin/users/{person.id}/staff-role/support"
    assert client.post(url, headers=auth_headers(support)).status_code == 403
    r = client.post(url, headers=auth_headers(admin))
    assert r.status_code == 200 and "support" in r.json()["roles"]
    assert (
        client.post(
            f"{API}/admin/users/{person.id}/staff-role/mechanic", headers=auth_headers(admin)
        ).status_code
        == 400
    )


def test_provider_analytics(client, db):
    _, mech, _ = _paid_sale(db)
    r = client.get(f"{API}/me/analytics", headers=auth_headers(mech))
    assert r.status_code == 200
    assert r.json()["period_days"] == 30
    customer = make_user(db, Role.VEHICLE_OWNER)
    assert client.get(f"{API}/me/analytics", headers=auth_headers(customer)).status_code == 403
