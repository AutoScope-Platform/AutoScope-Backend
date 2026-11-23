"""Youverify KYC driver (offline: httpx's MockTransport stands in for Youverify), name
matching, and the production guard."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.integrations import registry
from app.integrations.base import IntegrationError
from app.integrations.youverify import (
    YouverifyKyc,
    business_name_matches,
    person_name_matches,
)

BASE = "https://api.sandbox.youverify.co"


def make_kyc(handler) -> YouverifyKyc:
    settings = Settings(kyc_driver="youverify", youverify_api_key="yv-test-key")
    return YouverifyKyc(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))


def found(**data) -> httpx.Response:
    return httpx.Response(
        200, json={"success": True, "statusCode": 200, "data": {"status": "found", **data}}
    )


# --- Name matching ----------------------------------------------------------------------
def test_person_name_matches_in_any_order():
    assert person_name_matches("Adaora Okafor", "ADAORA", "OKAFOR")
    assert person_name_matches("Okafor Adaora Chioma", "Adaora", "Okafor")  # surname first
    assert not person_name_matches("Adaora Okafor", "Bisi", "Okafor")
    assert not person_name_matches("Adaora Okafor", None, "Okafor")


def test_business_name_ignores_ltd_and_the_like():
    assert business_name_matches("Okafor Motors", "OKAFOR MOTORS LIMITED")
    assert business_name_matches("Okafor Motors Ltd", "Okafor Motors Nigeria Enterprises")
    assert not business_name_matches("Okafor Motors", "Lagos Tyres Limited")
    assert not business_name_matches("Okafor Motors", None)


# --- NIN / driver's licence -------------------------------------------------------------
def test_nin_lookup_sends_key_and_consent_and_matches_name():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return found(firstName="ADAORA", middleName="CHIOMA", lastName="OKAFOR")

    result = make_kyc(handler).verify_person(
        id_type="nin", id_number="12345678901", full_name="Adaora Okafor"
    )
    assert result.passed and result.name == "ADAORA CHIOMA OKAFOR"
    assert str(seen[0].url) == f"{BASE}/v2/api/identity/ng/nin"
    assert seen[0].headers["token"] == "yv-test-key"
    assert json.loads(seen[0].content) == {"id": "12345678901", "isSubjectConsent": True}


def test_licence_with_another_name_is_flagged():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return found(firstName="BISI", lastName="ADEYEMI")

    result = make_kyc(handler).verify_person(
        id_type="drivers_license", id_number="ABC1234567890", full_name="Adaora Okafor"
    )
    assert not result.passed
    assert result.reason == "Name on Driver's licence (BISI ADEYEMI) does not match the account"
    assert seen == ["/v2/api/identity/ng/drivers-license"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            404, json={"success": False, "statusCode": 404, "name": "ResourceNotFoundError"}
        ),
        httpx.Response(200, json={"success": True, "data": {"status": "not_found"}}),
    ],
)
def test_unknown_nin_is_not_found(response):
    result = make_kyc(lambda request: response).verify_person(
        id_type="nin", id_number="12345678901", full_name="Adaora Okafor"
    )
    assert not result.passed and result.reason == "NIN not found"


def test_no_funds_or_bad_key_is_an_integration_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"success": False, "message": "Insufficient fund"})

    with pytest.raises(IntegrationError) as exc:
        make_kyc(handler).verify_person(id_type="nin", id_number="12345678901", full_name="A B")
    assert "402" in str(exc.value) and "Insufficient fund" in str(exc.value)


# --- CAC --------------------------------------------------------------------------------
def test_cac_lookup_checks_status_and_name():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return found(name="OKAFOR MOTORS LIMITED", companyStatus="ACTIVE")

    result = make_kyc(handler).verify_business(
        registration_number="RC1234567", business_name="Okafor Motors"
    )
    assert result.passed and result.name == "OKAFOR MOTORS LIMITED"
    assert str(seen[0].url) == f"{BASE}/v2/api/verifications/ng/company/basic"
    assert json.loads(seen[0].content) == {"registrationNumber": "RC1234567", "isConsent": True}


def test_inactive_or_other_company_is_flagged():
    inactive = make_kyc(lambda r: found(name="Okafor Motors Ltd", companyStatus="INACTIVE"))
    result = inactive.verify_business(registration_number="RC1", business_name="Okafor Motors")
    assert not result.passed and result.reason == "Company is inactive at CAC"

    other = make_kyc(lambda r: found(name="Lagos Tyres Limited", companyStatus="ACTIVE"))
    result = other.verify_business(registration_number="RC1", business_name="Okafor Motors")
    assert not result.passed and "does not match the business name" in result.reason


def test_document_images_pass_to_the_admin():
    result = make_kyc(lambda r: httpx.Response(500)).check_document(
        doc_type="government_id", storage_key="k", filename="id.jpg", full_name="A B"
    )
    assert result.passed


# --- Settings ---------------------------------------------------------------------------
def test_driver_needs_a_key():
    with pytest.raises(IntegrationError):
        YouverifyKyc(Settings(kyc_driver="youverify", youverify_api_key=None))


def test_registry_builds_youverify(monkeypatch):
    monkeypatch.setenv("KYC_DRIVER", "youverify")
    monkeypatch.setenv("YOUVERIFY_API_KEY", "yv-test-key")
    from app.core.config import get_settings

    get_settings.cache_clear()
    registry.reset()
    try:
        assert isinstance(registry.get_kyc(), YouverifyKyc)
    finally:
        get_settings.cache_clear()
        registry.reset()


def test_production_refuses_the_sandbox_address():
    live = {
        "env": "production", "jwt_secret": "x" * 40, "cors_origins": "https://admin.autoscope.ng",
        "sms_driver": "termii", "email_driver": "smtp", "push_driver": "fcm",
        "payment_driver": "paystack", "vin_driver": "nhtsa", "social_driver": "live",
        "storage_driver": "azure", "kyc_driver": "youverify",
    }  # fmt: skip
    with pytest.raises(ValueError, match="sandbox"):
        Settings(**live)
    Settings(**live, youverify_base_url="https://api.youverify.co")  # boots
