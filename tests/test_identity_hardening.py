"""Phase 1 identity fixes: OTP resend/cooldown, phone numbers, erasure proof, real file
types and per-user rate limits on listing creation and payments."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import BadRequest
from app.core.permissions import Role
from app.core.ratelimit import reset_counters
from app.core.uploads import sniff
from app.integrations import mock
from app.modules.identity import service
from app.modules.identity.models import KycStatus, OtpCode, OtpPurpose
from tests.factories import PASSWORD, auth_headers, make_user

API = "/api/v1"
OTP = get_settings().mock_otp_code
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"png" * 20


def _age_latest_code(db, user, purpose: OtpPurpose, seconds: int = 31) -> None:
    """Pretend the newest code was sent `seconds` ago (past the resend cooldown)."""
    otp = db.scalar(
        select(OtpCode)
        .where(OtpCode.user_id == user.id, OtpCode.purpose == purpose)
        .order_by(OtpCode.created_at.desc())
    )
    otp.created_at = otp.created_at - timedelta(seconds=seconds)
    db.commit()


# --- Sign-up code resend -----------------------------------------------------------------
def test_resend_never_reveals_whether_an_account_exists(client):
    body = {
        "full_name": "Ada Obi",
        "email": "fresh@example.com",
        "password": "Passw0rd!x",
        "accept_terms": True,
    }
    assert client.post(f"{API}/auth/register", json=body).status_code == 201
    # Within the 30 s cooldown: the old code answered 429 here, which leaked the account.
    known = client.post(f"{API}/auth/otp/resend", json={"identifier": "fresh@example.com"})
    unknown = client.post(f"{API}/auth/otp/resend", json={"identifier": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()


# --- Two-step sign-in --------------------------------------------------------------------
def _login(client, email):
    return client.post(f"{API}/auth/login", json={"identifier": email, "password": PASSWORD})


def test_second_factor_retry_reuses_code_and_relogin_works_at_once(client, db):
    make_user(db, Role.ADMIN, email="staff@autoscope.ng")
    first = _login(client, "staff@autoscope.ng")
    assert first.status_code == 200 and first.json()["requires_otp"]
    sent = len(mock.OUTBOX)

    # A double tap or retry within 30 s no longer fails, and does not text a second code.
    again = _login(client, "staff@autoscope.ng")
    assert again.status_code == 200 and again.json()["requires_otp"]
    assert len(mock.OUTBOX) == sent

    ok = client.post(
        f"{API}/auth/login/verify",
        json={"challenge_token": again.json()["challenge_token"], "code": OTP},
    )
    assert ok.status_code == 200

    # Signing in again straight away (e.g. a second device) gets a fresh code, not 429.
    relogin = _login(client, "staff@autoscope.ng")
    assert relogin.status_code == 200 and relogin.json()["requires_otp"]
    assert len(mock.OUTBOX) == sent + 1


def test_resend_sign_in_code(client, db):
    staff = make_user(db, Role.SUPPORT, email="support@autoscope.ng")
    challenge = _login(client, "support@autoscope.ng").json()["challenge_token"]

    too_soon = client.post(f"{API}/auth/login/resend", json={"challenge_token": challenge})
    assert too_soon.status_code == 429 and too_soon.json()["error"]["code"] == "otp_cooldown"

    _age_latest_code(db, staff, OtpPurpose.LOGIN)
    resent = client.post(f"{API}/auth/login/resend", json={"challenge_token": challenge})
    assert resent.status_code == 202 and resent.json() == {"otp_sent_to": "email"}
    ok = client.post(f"{API}/auth/login/verify", json={"challenge_token": challenge, "code": OTP})
    assert ok.status_code == 200

    bad = client.post(f"{API}/auth/login/resend", json={"challenge_token": "nonsense"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_challenge"


# --- Phone numbers -----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("08031234567", "+2348031234567"),
        ("8031234567", "+2348031234567"),  # typed without the leading 0
        ("0803 123 4567", "+2348031234567"),
        ("2348031234567", "+2348031234567"),
        ("+2348031234567", "+2348031234567"),
        ("+447911123456", "+447911123456"),
        ("00447911123456", "+447911123456"),
    ],
)
def test_phone_numbers_normalise(raw, expected):
    assert service.normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["447911123456", "12345", "abc"])
def test_ambiguous_phone_numbers_are_refused(raw):
    with pytest.raises(BadRequest) as exc:
        service.normalize_phone(raw)
    assert exc.value.code == "invalid_phone"


# --- Account erasure needs proof -----------------------------------------------------------
def _erase(client, headers, body):
    return client.request("DELETE", f"{API}/me", headers=headers, json=body)


def test_erasure_needs_password_or_code(client, db):
    user = make_user(db, email="leaving@example.com")
    h = auth_headers(user)
    assert client.request("DELETE", f"{API}/me", headers=h).status_code == 422
    assert _erase(client, h, {}).status_code == 422
    wrong = _erase(client, h, {"password": "not-it-123"})
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "invalid_password"
    db.refresh(user)
    assert user.erased_at is None

    sent = client.post(f"{API}/me/erase/code", headers=h)
    assert sent.status_code == 202 and sent.json() == {"sent_to": "email"}
    bad_code = _erase(client, h, {"code": "000000"})
    assert bad_code.status_code == 400 and bad_code.json()["error"]["code"] == "invalid_otp"
    assert _erase(client, h, {"code": OTP}).status_code == 204
    db.refresh(user)
    assert user.erased_at is not None and user.email is None


# --- Uploads are checked by their bytes ----------------------------------------------------
def test_sniff_recognises_accepted_types_only():
    assert sniff(JPEG) == "image/jpeg"
    assert sniff(PNG) == "image/png"
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff(b"%PDF-1.7\n") == "application/pdf"
    assert sniff(b"MZ\x90\x00 an exe") is None
    assert sniff(b"") is None


def _kyc_upload(client, user, data, content_type, filename="doc"):
    return client.post(
        f"{API}/me/kyc-documents",
        headers=auth_headers(user),
        data={"doc_type": "government_id"},
        files={"file": (filename, data, content_type)},
    )


def test_kyc_upload_uses_real_file_type(client, db):
    garage = make_user(db, Role.MECHANIC)
    disguised = _kyc_upload(client, garage, b"MZ\x90\x00 really an exe", "image/png", "id.png")
    assert disguised.status_code == 400
    assert disguised.json()["error"]["code"] == "unsupported_file_type"

    # A PNG labelled as JPEG is stored as what it really is.
    mislabelled = _kyc_upload(client, garage, PNG, "image/jpeg", "id.jpg")
    assert mislabelled.status_code == 201
    assert mislabelled.json()["documents"][0]["content_type"] == "image/png"


def test_listing_photo_rejects_disguised_file(client, db):
    dealer = make_user(db, Role.DEALER, kyc=KycStatus.APPROVED)
    h = auth_headers(dealer)
    listing = client.post(
        f"{API}/listings",
        headers=h,
        json={
            "type": "vehicle",
            "title": "2018 Toyota Camry",
            "price_minor": 950_000_000,
            "attributes": {"make": "Toyota", "model": "Camry", "year": 2018, "mileage_km": 1},
        },
    ).json()
    bad = client.post(
        f"{API}/listings/{listing['id']}/images",
        headers=h,
        files={"file": ("car.jpg", b"MZ\x90\x00 not a photo", "image/jpeg")},
    )
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_file_type"
    good = client.post(
        f"{API}/listings/{listing['id']}/images",
        headers=h,
        files={"file": ("car.jpg", JPEG, "image/jpeg")},
    )
    assert good.status_code == 201


# --- Per-user rate limits on listing creation and payments ---------------------------------
@pytest.fixture
def rate_limits_on(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    get_settings.cache_clear()
    reset_counters()
    yield
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()
    reset_counters()


def test_listing_creation_is_limited_per_user_not_per_ip(client, db, rate_limits_on):
    def create(user):
        return client.post(
            f"{API}/listings",
            headers=auth_headers(user),
            json={
                "type": "part",
                "title": "Brake pads",
                "price_minor": 1_500_000,
                "stock": 3,
                "attributes": {"brand": "Bosch", "condition": "new"},
            },
        ).status_code

    seller = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED)
    statuses = [create(seller) for _ in range(11)]
    assert statuses[:10] == [201] * 10 and statuses[10] == 429
    # Someone else on the same IP (same carrier network) is not blocked.
    neighbour = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED)
    assert create(neighbour) == 201


def test_payment_start_is_rate_limited(client, db, rate_limits_on):
    buyer = make_user(db)
    statuses = [
        client.post(
            f"{API}/payments",
            headers={**auth_headers(buyer), "Idempotency-Key": f"k-{i}"},
            json={"transaction_id": "00000000-0000-0000-0000-000000000000"},
        ).status_code
        for i in range(11)
    ]
    assert 429 not in statuses[:10] and statuses[10] == 429
