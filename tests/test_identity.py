"""Identity module tests: sign-up, sign-in, tokens, passwords, roles and KYC."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.config import get_settings
from app.core.permissions import Role
from app.core.ratelimit import reset_counters
from app.integrations import mock, registry
from app.integrations.base import IntegrationError
from app.modules.identity import service
from app.modules.identity.models import (
    IdCheckStatus,
    KycDocType,
    KycStatus,
    KycSubmission,
    KycSubmissionStatus,
    User,
)
from tests.factories import PASSWORD, auth_headers, make_user

OTP = get_settings().mock_otp_code


def register_payload(**overrides):
    body = {
        "full_name": "Ada Obi",
        "email": "ada@example.com",
        "password": "Passw0rd!x",
        "roles": ["vehicle_owner"],
        "accept_terms": True,
        "city": "Lagos",
    }
    body.update(overrides)
    return body


# --- Registration ----------------------------------------------------------------------
def test_register_verify_and_read_profile(client):
    r = client.post("/api/v1/auth/register", json=register_payload())
    assert r.status_code == 201
    assert r.json()["verification_sent_to"] == "email"
    assert mock.OUTBOX[-1].to == "ada@example.com"

    # Cannot sign in before the code is confirmed.
    r = client.post(
        "/api/v1/auth/login", json={"identifier": "ada@example.com", "password": "Passw0rd!x"}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "contact_not_verified"

    r = client.post("/api/v1/auth/otp/verify", json={"identifier": "ada@example.com", "code": OTP})
    assert r.status_code == 200
    token = r.json()["access_token"]

    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["email"] == "ada@example.com"
    assert me["roles"] == ["vehicle_owner"]
    assert "purchase" in me["permissions"] and "admin_console" not in me["permissions"]


def test_register_by_phone_normalises_number_and_sends_sms(client):
    r = client.post(
        "/api/v1/auth/register", json=register_payload(email=None, phone="0803 123 4567")
    )
    assert r.status_code == 201 and r.json()["verification_sent_to"] == "phone"
    assert mock.OUTBOX[-1].channel == "sms" and mock.OUTBOX[-1].to == "+2348031234567"


def test_register_rejects_duplicates_staff_roles_and_weak_input(client):
    assert client.post("/api/v1/auth/register", json=register_payload()).status_code == 201
    dup = client.post("/api/v1/auth/register", json=register_payload())
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "already_registered"

    staff = client.post(
        "/api/v1/auth/register", json=register_payload(email="x@example.com", roles=["admin"])
    )
    assert staff.status_code == 422
    weak = client.post(
        "/api/v1/auth/register", json=register_payload(email="y@example.com", password="short")
    )
    assert weak.status_code == 422 and weak.json()["error"]["code"] == "validation_error"
    no_contact = client.post("/api/v1/auth/register", json=register_payload(email=None))
    assert no_contact.status_code == 422
    no_business = client.post(
        "/api/v1/auth/register", json=register_payload(email="z@example.com", roles=["mechanic"])
    )
    assert no_business.status_code == 422  # business accounts need a business name


def test_business_registration_starts_kyc_flow(client, db):
    body = register_payload(
        email="garage@example.com", roles=["mechanic"], business_name="Ada Garage"
    )
    assert client.post("/api/v1/auth/register", json=body).status_code == 201
    user = db.scalar(select(User).where(User.email == "garage@example.com"))
    assert user.kyc_status == KycStatus.NOT_SUBMITTED


# --- Sign-in ---------------------------------------------------------------------------
def test_login_returns_tokens_and_audits(client, db):
    user = make_user(db, email="owner@example.com")
    r = client.post(
        "/api/v1/auth/login", json={"identifier": "owner@example.com", "password": PASSWORD}
    )
    assert r.status_code == 200 and r.json()["tokens"]["access_token"]
    actions = [a.action for a in db.scalars(select(AuditLog).where(AuditLog.actor_id == user.id))]
    assert "Signed in" in actions


def test_wrong_password_is_generic_and_account_locks(client, db):
    make_user(db, email="lock@example.com")
    for _ in range(get_settings().login_max_failures):
        r = client.post(
            "/api/v1/auth/login", json={"identifier": "lock@example.com", "password": "nope1234"}
        )
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r = client.post(
        "/api/v1/auth/login", json={"identifier": "lock@example.com", "password": PASSWORD}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "account_locked"


def test_unknown_account_gets_the_same_error(client):
    r = client.post(
        "/api/v1/auth/login", json={"identifier": "ghost@example.com", "password": "whatever1"}
    )
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"


def test_admin_login_needs_otp(client, db):
    admin = make_user(db, Role.ADMIN, email="adaora@autoscope.ng", name="Adaora Nwankwo")
    r = client.post(
        "/api/v1/auth/login", json={"identifier": "adaora@autoscope.ng", "password": PASSWORD}
    )
    body = r.json()
    assert r.status_code == 200 and body["requires_otp"] is True and body["tokens"] is None

    # A challenge token is not an access token.
    bad = client.get("/api/v1/me", headers={"Authorization": f"Bearer {body['challenge_token']}"})
    assert bad.status_code == 401

    wrong = client.post(
        "/api/v1/auth/login/verify",
        json={"challenge_token": body["challenge_token"], "code": "000000"},
    )
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "invalid_otp"

    ok = client.post(
        "/api/v1/auth/login/verify", json={"challenge_token": body["challenge_token"], "code": OTP}
    )
    assert ok.status_code == 200
    me = client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {ok.json()['access_token']}"}
    ).json()
    assert "admin_console" in me["permissions"] and "admin_finance" in me["permissions"]
    actions = [a.action for a in db.scalars(select(AuditLog).where(AuditLog.actor_id == admin.id))]
    assert "Failed sign-in (wrong OTP)" in actions and "Signed in" in actions


def test_two_factor_can_be_enabled_by_users_but_not_disabled_by_staff(client, db):
    owner = make_user(db)
    r = client.put("/api/v1/me/two-factor", json={"enabled": True}, headers=auth_headers(owner))
    assert r.status_code == 200 and r.json()["two_factor_enabled"] is True
    admin = make_user(db, Role.SUPPORT)
    r = client.put("/api/v1/me/two-factor", json={"enabled": False}, headers=auth_headers(admin))
    assert r.status_code == 403


# --- Tokens ----------------------------------------------------------------------------
def login_tokens(client, identifier, password=PASSWORD):
    r = client.post("/api/v1/auth/login", json={"identifier": identifier, "password": password})
    return r.json()["tokens"]


def test_refresh_rotates_and_reuse_revokes_everything(client, db):
    make_user(db, email="rot@example.com")
    first = login_tokens(client, "rot@example.com")
    second = client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert second.status_code == 200
    new_refresh = second.json()["refresh_token"]
    assert new_refresh != first["refresh_token"]

    # Replaying the old token is treated as theft: the new token dies too.
    replay = client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401
    after = client.post("/api/v1/auth/refresh", json={"refresh_token": new_refresh})
    assert after.status_code == 401


def test_staff_session_expires_after_idle_period(client, db):
    from datetime import timedelta

    from app.modules.identity.models import RefreshToken

    make_user(db, Role.SUPPORT, email="kunle@autoscope.ng")
    login = client.post(
        "/api/v1/auth/login", json={"identifier": "kunle@autoscope.ng", "password": PASSWORD}
    ).json()
    tokens = client.post(
        "/api/v1/auth/login/verify", json={"challenge_token": login["challenge_token"], "code": OTP}
    ).json()
    row = db.scalar(select(RefreshToken).order_by(RefreshToken.created_at.desc()))
    row.last_used_at = row.last_used_at - timedelta(minutes=45)
    db.commit()
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "session_expired"


def test_logout_revokes_refresh_token(client, db):
    make_user(db, email="out@example.com")
    tokens = login_tokens(client, "out@example.com")
    assert (
        client.post(
            "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )


def test_protected_routes_reject_missing_or_bad_tokens_in_shared_error_shape(client):
    r = client.get("/api/v1/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "not_authenticated"
    r = client.get("/api/v1/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token"


def test_suspended_user_is_locked_out(client, db):
    admin = make_user(db, Role.ADMIN)
    target = make_user(db, email="bad@example.com")
    headers = auth_headers(target)
    assert client.get("/api/v1/me", headers=headers).status_code == 200
    service.set_account_status(
        db, target.id, service.AccountStatus.SUSPENDED, admin=admin, reason=None, ip=None
    )
    r = client.get("/api/v1/me", headers=headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "account_suspended"


# --- Passwords -------------------------------------------------------------------------
def test_password_reset_flow_signs_out_other_sessions(client, db):
    make_user(db, email="reset@example.com")
    old = login_tokens(client, "reset@example.com")
    assert (
        client.post(
            "/api/v1/auth/password/forgot", json={"identifier": "reset@example.com"}
        ).status_code
        == 202
    )
    # Unknown accounts get the same answer.
    assert (
        client.post(
            "/api/v1/auth/password/forgot", json={"identifier": "nobody@example.com"}
        ).status_code
        == 202
    )

    r = client.post(
        "/api/v1/auth/password/reset",
        json={"identifier": "reset@example.com", "code": OTP, "new_password": "NewPassw0rd"},
    )
    assert r.status_code == 204
    assert (
        client.post(
            "/api/v1/auth/refresh", json={"refresh_token": old["refresh_token"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"identifier": "reset@example.com", "password": "NewPassw0rd"},
        ).status_code
        == 200
    )


def test_otp_locks_after_too_many_wrong_guesses(client, db):
    make_user(db, email="guess@example.com")
    client.post("/api/v1/auth/password/forgot", json={"identifier": "guess@example.com"})
    body = {"identifier": "guess@example.com", "code": "111111", "new_password": "NewPassw0rd"}
    codes = [
        client.post("/api/v1/auth/password/reset", json=body).json()["error"]["code"]
        for _ in range(get_settings().otp_max_attempts)
    ]
    assert codes == ["invalid_otp"] * get_settings().otp_max_attempts
    locked = client.post("/api/v1/auth/password/reset", json={**body, "code": OTP})
    assert locked.status_code == 429 and locked.json()["error"]["code"] == "otp_locked"


def test_change_password_requires_current_password(client, db):
    user = make_user(db)
    h = auth_headers(user)
    bad = client.post(
        "/api/v1/me/password",
        json={"current_password": "wrong", "new_password": "NewPassw0rd"},
        headers=h,
    )
    assert bad.status_code == 400
    ok = client.post(
        "/api/v1/me/password",
        json={"current_password": PASSWORD, "new_password": "NewPassw0rd"},
        headers=h,
    )
    assert ok.status_code == 204


# --- Roles and profile -----------------------------------------------------------------
def test_add_business_role_needs_business_name_and_starts_kyc(client, db):
    owner = make_user(db)
    h = auth_headers(owner)
    r = client.post("/api/v1/me/roles", json={"role": "mechanic"}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "business_name_required"
    client.patch("/api/v1/me", json={"business_name": "Ada Motors"}, headers=h)
    r = client.post("/api/v1/me/roles", json={"role": "mechanic"}, headers=h)
    assert r.status_code == 200 and set(r.json()["roles"]) == {"vehicle_owner", "mechanic"}
    assert r.json()["kyc_status"] == "not_submitted"
    assert client.post("/api/v1/me/roles", json={"role": "admin"}, headers=h).status_code == 403


def test_erase_account_anonymises_and_blocks_sign_in(client, db):
    user = make_user(db, email="gone@example.com")
    h = auth_headers(user)
    r = client.request("DELETE", "/api/v1/me", headers=h, json={"password": PASSWORD})
    assert r.status_code == 204
    db.refresh(user)
    assert user.email is None and user.full_name == "Erased user" and user.erased_at is not None
    assert client.get("/api/v1/me", headers=h).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login", json={"identifier": "gone@example.com", "password": PASSWORD}
        ).status_code
        == 401
    )


# --- KYC -------------------------------------------------------------------------------
def upload(client, user, doc_type, filename="doc.jpg"):
    return client.post(
        "/api/v1/me/kyc-documents",
        headers=auth_headers(user),
        data={"doc_type": doc_type.value},
        files={"file": (filename, b"\xff\xd8\xfffake-image", "image/jpeg")},
    )


def test_kyc_needs_all_documents_then_queues_for_review(client, db):
    garage = make_user(db, Role.MECHANIC)
    r = upload(client, garage, KycDocType.GOVERNMENT_ID)
    assert r.status_code == 201 and r.json()["status"] == "incomplete"
    assert set(r.json()["missing_documents"]) == {"business_registration", "licence"}
    upload(client, garage, KycDocType.BUSINESS_REGISTRATION)
    r = upload(client, garage, KycDocType.LICENCE)
    body = r.json()
    assert (
        body["status"] == "pending"
        and body["id_check_status"] == "passed"
        and body["missing_documents"] == []
    )
    db.refresh(garage)
    assert garage.kyc_status == KycStatus.PENDING
    # Cannot change documents while under review.
    assert upload(client, garage, KycDocType.LICENCE).status_code == 409


def test_flagged_document_is_reported_to_reviewers(client, db):
    garage = make_user(db, Role.HAULAGE_PROVIDER)
    upload(client, garage, KycDocType.GOVERNMENT_ID)
    upload(client, garage, KycDocType.BUSINESS_REGISTRATION)
    r = upload(client, garage, KycDocType.LICENCE, filename="licence-blurred.jpg")
    assert r.json()["id_check_status"] == IdCheckStatus.FLAGGED
    assert r.json()["id_check_reason"] == "Licence image is blurred"


def set_details(client, user, **body):
    return client.put(
        "/api/v1/me/kyc-details", headers=auth_headers(user), json={"consent": True, **body}
    )


def upload_all(client, user):
    upload(client, user, KycDocType.GOVERNMENT_ID)
    upload(client, user, KycDocType.BUSINESS_REGISTRATION)
    return upload(client, user, KycDocType.LICENCE)


def test_id_and_cac_numbers_are_checked_when_submitted(client, db):
    garage = make_user(db, Role.MECHANIC, name="Adaora Okafor", business_name="Okafor Motors")
    r = set_details(
        client, garage, id_type="nin", id_number="1234 5678 901", registration_number="1234567"
    )
    assert r.status_code == 200, r.text
    assert r.json()["id_number_last4"] == "8901" and r.json()["registration_number"] == "RC1234567"

    body = upload_all(client, garage).json()
    assert body["status"] == "pending" and body["id_check_status"] == "passed"
    assert body["id_check_reason"] is None
    assert body["verified_person_name"] == "Adaora Okafor"
    assert body["verified_business_name"] == "Okafor Motors"

    # Only the last 4 digits of the NIN are kept once checked.
    submission = db.scalar(select(KycSubmission).where(KycSubmission.user_id == garage.id))
    assert submission.id_number is None and submission.id_number_last4 == "8901"
    # Numbers cannot change while under review.
    assert set_details(client, garage, registration_number="RC7654321").status_code == 409


def test_unknown_numbers_flag_the_submission(client, db):
    garage = make_user(db, Role.DEALER)
    set_details(
        client, garage, id_type="nin", id_number="00012345678", registration_number="RC0000000"
    )
    body = upload_all(client, garage).json()
    assert body["id_check_status"] == "flagged"
    assert body["id_check_reason"] == "NIN not found; CAC number not found"


def test_without_numbers_the_admin_is_told_to_check_by_hand(client, db):
    garage = make_user(db, Role.HAULAGE_PROVIDER)
    body = upload_all(client, garage).json()
    assert body["id_check_status"] == "passed"
    assert body["id_check_reason"] == (
        "No ID number or CAC number given: check the documents by hand"
    )


def test_unavailable_id_service_flags_for_a_manual_check(client, db):
    class DownKyc(mock.MockKyc):
        def verify_person(self, **kwargs):
            raise IntegrationError("Youverify could not run the check (503)")

    registry.override("kyc", DownKyc())
    try:
        garage = make_user(db, Role.MECHANIC)
        set_details(client, garage, id_type="nin", id_number="12345678901")
        body = upload_all(client, garage).json()
    finally:
        registry.override("kyc", None)
    assert body["status"] == "pending" and body["id_check_status"] == "flagged"
    assert body["id_check_reason"].startswith("Automated check unavailable, check by hand")


@pytest.mark.parametrize(
    "body",
    [
        {"id_type": "nin", "id_number": "12345"},  # a NIN is 11 digits
        {"id_number": "12345678901"},  # no ID type
        {"registration_number": "hello"},
        {"id_type": "nin", "id_number": "12345678901", "consent": False},
        {},
    ],
)
def test_kyc_details_validation(client, db, body):
    garage = make_user(db, Role.MECHANIC)
    r = client.put(
        "/api/v1/me/kyc-details", headers=auth_headers(garage), json={"consent": True, **body}
    )
    assert r.status_code == 422


def test_owners_do_not_give_kyc_details(client, db):
    owner = make_user(db)
    assert set_details(client, owner, registration_number="RC1234567").status_code == 403


def test_kyc_upload_rules(client, db):
    owner = make_user(db)
    assert (
        upload(client, owner, KycDocType.GOVERNMENT_ID).status_code == 403
    )  # owners do not need KYC
    garage = make_user(db, Role.DEALER)
    bad_type = client.post(
        "/api/v1/me/kyc-documents",
        headers=auth_headers(garage),
        data={"doc_type": "licence"},
        files={"file": ("x.exe", b"MZ", "application/x-msdownload")},
    )
    assert (
        bad_type.status_code == 400 and bad_type.json()["error"]["code"] == "unsupported_file_type"
    )


def test_admin_decision_updates_user_and_audit_log(db):
    garage = make_user(db, Role.DEALER)
    admin = make_user(db, Role.SUPPORT, name="Kunle Bakare")
    for doc in KycDocType:
        submission = service.upload_kyc_document(
            db, garage, doc, f"{doc.value}.jpg", "image/jpeg", b"\xff\xd8\xffx"
        )
    assert submission.status == KycSubmissionStatus.PENDING

    # A rejection must say why (checked at the API layer by KycDecisionIn); approve here.
    service.decide_kyc(db, submission.id, approve=True, reason=None, admin=admin, ip="1.2.3.4")
    db.refresh(garage)
    assert garage.kyc_status == KycStatus.APPROVED
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "Approved KYC"))
    assert entry.target == submission.ref and entry.actor_name == "Kunle Bakare"
    with pytest.raises(service.UnprocessableEntity):
        service.decide_kyc(db, submission.id, approve=False, reason="late", admin=admin, ip=None)


# --- Rate limiting ---------------------------------------------------------------------
def test_login_is_rate_limited(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    get_settings.cache_clear()
    reset_counters()
    try:
        statuses = [
            client.post(
                "/api/v1/auth/login", json={"identifier": "a@example.com", "password": "x"}
            ).status_code
            for _ in range(22)
        ]
        assert statuses[-1] == 429
    finally:
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
        get_settings.cache_clear()
        reset_counters()
