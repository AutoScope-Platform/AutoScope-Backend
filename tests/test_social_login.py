"""Google / Apple sign-in (FR-1.2): the API flow with the mock verifier, and the live
verifier's token checks with locally signed tokens (no network)."""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select

from app.core.audit import AuditLog
from app.core.config import Settings, get_settings
from app.core.permissions import Role
from app.integrations import registry
from app.integrations.base import IntegrationError, SocialIdentity, SocialTokenError
from app.integrations.social import APPLE_ISSUER, LiveSocialVerifier
from app.modules.identity.models import AccountStatus, OtpCode, OtpPurpose, SocialAccount, User
from tests.factories import PASSWORD, auth_headers, make_user

API = "/api/v1"
OTP = get_settings().mock_otp_code


def social(client, token, provider="google", **extra):
    return client.post(
        f"{API}/auth/social", json={"provider": provider, "id_token": token, **extra}
    )


def count(db, model, *where):
    return db.scalar(select(func.count()).select_from(model).where(*where))


# --- API flow (mock verifier) --------------------------------------------------------------
def test_first_sign_in_asks_for_details_then_creates_account(client, db):
    token = "mock:g-100:chidi@gmail.com:Chidi Eze"
    first = social(client, token)
    assert first.status_code == 422
    err = first.json()["error"]
    assert err["code"] == "signup_details_required"
    assert err["details"] == {
        "missing": ["accept_terms"],
        "email": "chidi@gmail.com",
        "name": "Chidi Eze",
    }
    assert count(db, User, User.email == "chidi@gmail.com") == 0

    created = social(client, token, accept_terms=True, city="Enugu")
    assert created.status_code == 200
    access = created.json()["tokens"]["access_token"]
    me = client.get(f"{API}/me", headers={"Authorization": f"Bearer {access}"}).json()
    assert me["email"] == "chidi@gmail.com" and me["full_name"] == "Chidi Eze"
    assert me["roles"] == ["vehicle_owner"] and me["has_password"] is False
    logs = db.scalars(select(AuditLog).where(AuditLog.actor_id == me["id"]))
    actions = [a.action for a in logs]
    assert "Account created (google)" in actions and "Signed in (google)" in actions

    # Returning user: the token alone is enough, and no second account is made.
    again = social(client, token)
    assert again.status_code == 200 and again.json()["tokens"]["user"]["id"] == me["id"]
    assert count(db, SocialAccount) == 1


def test_business_role_needs_business_name(client, db):
    token = "mock:a-200:garage@icloud.com:"
    missing = social(client, token, provider="apple", accept_terms=True, roles=["mechanic"])
    assert missing.status_code == 422
    assert missing.json()["error"]["details"]["missing"] == ["business_name"]
    ok = social(
        client,
        token,
        provider="apple",
        accept_terms=True,
        roles=["mechanic"],
        business_name="Eze Motors",
        full_name="Ngozi Eze",
    )
    user = ok.json()["tokens"]["user"]
    assert ok.status_code == 200 and user["kyc_status"] == "not_submitted"
    assert user["full_name"] == "Ngozi Eze"  # Apple gives the name to the app only
    assert social(client, token, provider="apple", roles=["admin"]).status_code == 422


def test_verified_email_links_to_existing_password_account(client, db):
    owner = make_user(db, email="ada@example.com")
    r = social(client, "mock:g-300:ADA@example.com:Ada")
    assert r.status_code == 200 and r.json()["tokens"]["user"]["id"] == str(owner.id)
    link = db.scalar(select(SocialAccount).where(SocialAccount.user_id == owner.id))
    assert link.provider == "google" and link.subject == "g-300"
    logs = db.scalars(select(AuditLog).where(AuditLog.actor_id == owner.id))
    assert "Linked google sign-in" in [a.action for a in logs]


def test_unverified_email_does_not_take_over_an_account(client, db):
    make_user(db, email="victim@example.com")

    class Unverified:
        def verify(self, provider, id_token, nonce=None):
            return SocialIdentity(provider, "x-1", "victim@example.com", email_verified=False)

    registry.override("social", Unverified())
    r = social(client, "anything-at-all")
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_registered"


def test_staff_and_2fa_users_still_need_the_code(client, db):
    make_user(db, Role.ADMIN, email="adaora@autoscope.ng")
    r = social(client, "mock:g-400:adaora@autoscope.ng:Adaora")
    body = r.json()
    assert r.status_code == 200 and body["requires_otp"] and body["tokens"] is None
    ok = client.post(
        f"{API}/auth/login/verify",
        json={"challenge_token": body["challenge_token"], "code": OTP},
    )
    assert ok.status_code == 200


def test_suspended_account_cannot_use_social_sign_in(client, db):
    user = make_user(db, email="banned@example.com")
    user.status = AccountStatus.SUSPENDED
    db.commit()
    r = social(client, "mock:g-500:banned@example.com:")
    assert r.status_code == 403 and r.json()["error"]["code"] == "account_suspended"


def test_bad_token_and_provider_outage(client):
    r = social(client, "not-a-real-token")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_social_token"

    class Down:
        def verify(self, provider, id_token, nonce=None):
            raise IntegrationError("cannot reach Google")

    registry.override("social", Down())
    r = social(client, "whatever-token")
    assert r.status_code == 503 and r.json()["error"]["code"] == "social_login_unavailable"


def test_social_account_sets_password_by_reset_and_erases_with_code(client, db):
    first = social(client, "mock:g-600:obi@gmail.com:Obi", accept_terms=True)
    h = {"Authorization": f"Bearer {first.json()['tokens']['access_token']}"}

    no_pw = client.post(
        f"{API}/me/password",
        headers=h,
        json={"current_password": "x", "new_password": "NewPassw0rd"},
    )
    assert no_pw.status_code == 400 and no_pw.json()["error"]["code"] == "no_password_set"

    client.post(f"{API}/auth/password/forgot", json={"identifier": "obi@gmail.com"})
    reset = client.post(
        f"{API}/auth/password/reset",
        json={"identifier": "obi@gmail.com", "code": OTP, "new_password": "NewPassw0rd"},
    )
    assert reset.status_code == 204
    login = client.post(
        f"{API}/auth/login", json={"identifier": "obi@gmail.com", "password": "NewPassw0rd"}
    )
    assert login.status_code == 200 and login.json()["tokens"]["user"]["has_password"] is True

    h = {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}
    client.post(f"{API}/me/erase/code", headers=h)
    erased = client.request("DELETE", f"{API}/me", headers=h, json={"code": OTP})
    assert erased.status_code == 204
    assert count(db, SocialAccount) == 0
    # Signing in with Google again starts over instead of reviving the erased account.
    assert social(client, "mock:g-600:obi@gmail.com:Obi").status_code == 422
    codes = db.scalars(select(OtpCode).where(OtpCode.purpose == OtpPurpose.ACCOUNT_ERASURE))
    assert all(c.consumed_at is not None for c in codes)


def test_password_users_can_still_erase_with_password(client, db):
    user = make_user(db)
    r = client.request(
        "DELETE", f"{API}/me", headers=auth_headers(user), json={"password": PASSWORD}
    )
    assert r.status_code == 204


# --- Live verifier: token checks without the network ----------------------------------------
GOOGLE_IDS = "web.apps.googleusercontent.com,ios.apps.googleusercontent.com"
APPLE_ID = "ng.autoscope.app"


@pytest.fixture(scope="module")
def apple_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _settings(**overrides) -> Settings:
    values = {"google_client_ids": GOOGLE_IDS, "apple_client_ids": APPLE_ID}
    return Settings(**{**values, **overrides})


def _apple_token(key, **claims) -> str:
    now = int(time.time())
    payload = {
        "iss": APPLE_ISSUER,
        "aud": APPLE_ID,
        "sub": "001234.abcd",
        "iat": now,
        "exp": now + 600,
        "email": "Rel@privaterelay.appleid.com",
        "email_verified": "true",
        **claims,
    }
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "test-key"})


def _apple_verifier(key, **settings) -> LiveSocialVerifier:
    signing_key = SimpleNamespace(key=key.public_key())
    jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _token: signing_key)
    return LiveSocialVerifier(_settings(**settings), apple_jwks=jwks)


def test_apple_token_checks(apple_key):
    v = _apple_verifier(apple_key)
    identity = v.verify("apple", _apple_token(apple_key))
    assert identity.subject == "001234.abcd"
    assert identity.email == "rel@privaterelay.appleid.com" and identity.email_verified

    # Apple carries the SHA-256 of the app's nonce; the raw nonce from the app must match it.
    hashed = hashlib.sha256(b"n-123").hexdigest()
    assert v.verify("apple", _apple_token(apple_key, nonce=hashed), nonce="n-123")
    bad_tokens = [
        (_apple_token(apple_key, nonce=hashed), "other"),
        (_apple_token(apple_key, aud="com.someone.else"), None),
        (_apple_token(apple_key, iss="https://evil.example"), None),
        (_apple_token(apple_key, exp=int(time.time()) - 3600), None),
        (_apple_token(rsa.generate_private_key(public_exponent=65537, key_size=2048)), None),
    ]
    for token, nonce in bad_tokens:
        with pytest.raises(SocialTokenError):
            v.verify("apple", token, nonce=nonce)
    with pytest.raises(IntegrationError):
        _apple_verifier(apple_key, apple_client_ids="").verify("apple", _apple_token(apple_key))


def test_google_token_checks():
    claims = {
        "aud": "ios.apps.googleusercontent.com",
        "sub": "1098",
        "email": "Ada@Gmail.com",
        "email_verified": True,
        "name": "Ada Obi",
        "nonce": "n-1",
    }
    v = LiveSocialVerifier(_settings(), google_verify=lambda _token: dict(claims))
    identity = v.verify("google", "token", nonce="n-1")
    assert (identity.subject, identity.email, identity.name) == ("1098", "ada@gmail.com", "Ada Obi")
    with pytest.raises(SocialTokenError):
        v.verify("google", "token", nonce="n-2")

    other_app = LiveSocialVerifier(
        _settings(), google_verify=lambda _token: {**claims, "aud": "someone-else"}
    )
    with pytest.raises(SocialTokenError):
        other_app.verify("google", "token")
    with pytest.raises(IntegrationError):
        LiveSocialVerifier(_settings(google_client_ids="")).verify("google", "token")
    with pytest.raises(SocialTokenError):
        v.verify("facebook", "token")
