"""Youverify KYC driver (Nigeria): NIN, driver's licence and CAC business lookups.

Youverify checks ID *numbers* against government records; it cannot check an uploaded
document photo from a server (its document capture is a web-only SDK). So uploaded
documents pass through to the admin, and the numbers the provider types in are checked
here. A failed or unavailable check flags the submission; the admin decides.

Settings: `KYC_DRIVER=youverify`, `YOUVERIFY_API_KEY` (dashboard: API & Webhooks; sandbox
and live keys differ) and `YOUVERIFY_BASE_URL` (sandbox by default).
Docs: https://doc.youverify.co/ (Quick Check: KYC Nigeria, KYB Nigeria).
"""

from __future__ import annotations

import re

import httpx

from app.core.config import Settings
from app.integrations.base import IntegrationError, KycCheckResult

PERSON_PATHS = {
    "nin": "/v2/api/identity/ng/nin",
    "drivers_license": "/v2/api/identity/ng/drivers-license",
}
PERSON_LABELS = {"nin": "NIN", "drivers_license": "Driver's licence"}
BUSINESS_PATH = "/v2/api/verifications/ng/company/basic"

# Words that say nothing about which company it is ("Okafor Motors Ltd" = "OKAFOR MOTORS LIMITED").
_COMPANY_FILLER = {
    "limited", "ltd", "plc", "nigeria", "nig", "ng", "company", "co", "and", "the", "of",
    "enterprise", "enterprises", "ventures", "services", "global", "international", "intl",
}  # fmt: skip


def _words(text: str | None) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def person_name_matches(account_name: str, first_name: str | None, last_name: str | None) -> bool:
    """True when the record's first and last names both appear in the account name, in any
    order (records are often SURNAME FIRST, and middle names vary)."""
    account = set(_words(account_name))
    first, last = _words(first_name), _words(last_name)
    return bool(first and last) and set(first) <= account and set(last) <= account


def business_name_matches(business_name: str, registered_name: str | None) -> bool:
    """True when most of the distinctive words are shared, ignoring Ltd/Enterprises and the like."""
    ours = set(_words(business_name)) - _COMPANY_FILLER
    theirs = set(_words(registered_name)) - _COMPANY_FILLER
    if not ours or not theirs:
        return False
    return len(ours & theirs) / min(len(ours), len(theirs)) >= 0.6


class YouverifyKyc:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not settings.youverify_api_key:
            raise IntegrationError("YOUVERIFY_API_KEY is not set (see docs/INTEGRATIONS.md)")
        self._key = settings.youverify_api_key
        self._base = settings.youverify_base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30)

    def _post(self, path: str, body: dict) -> dict | None:
        """Run one lookup. Returns the record, None when it was not found, and raises
        IntegrationError when Youverify could not answer (bad key, no funds, outage)."""
        try:
            response = self._client.post(
                f"{self._base}{path}", json=body, headers={"token": self._key}
            )
        except httpx.HTTPError as exc:
            raise IntegrationError(f"Youverify request failed: {exc}") from exc
        if response.status_code == 404:
            return None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.is_error:
            message = payload.get("message") if isinstance(payload, dict) else None
            raise IntegrationError(
                f"Youverify could not run the check ({response.status_code}): "
                f"{message or response.text[:120]}"
            )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or str(data.get("status", "")).lower() != "found":
            return None
        return data

    def check_document(
        self, *, doc_type: str, storage_key: str, filename: str, full_name: str
    ) -> KycCheckResult:
        # Youverify has no server-side photo check: the admin reviews the images.
        return KycCheckResult(True)

    def verify_person(self, *, id_type: str, id_number: str, full_name: str) -> KycCheckResult:
        if id_type not in PERSON_PATHS:
            raise IntegrationError(f"Unsupported ID type: {id_type}")
        label = PERSON_LABELS[id_type]
        record = self._post(PERSON_PATHS[id_type], {"id": id_number, "isSubjectConsent": True})
        if record is None:
            return KycCheckResult(False, f"{label} not found")
        first, last = record.get("firstName"), record.get("lastName")
        name = " ".join(p for p in (first, record.get("middleName"), last) if p) or None
        if not person_name_matches(full_name, first, last):
            return KycCheckResult(
                False, f"Name on {label} ({name or 'not given'}) does not match the account", name
            )
        return KycCheckResult(True, name=name)

    def verify_business(self, *, registration_number: str, business_name: str) -> KycCheckResult:
        record = self._post(
            BUSINESS_PATH, {"registrationNumber": registration_number, "isConsent": True}
        )
        if record is None:
            return KycCheckResult(False, "CAC number not found")
        name = record.get("name")
        status = str(record.get("companyStatus") or "").strip()
        if status and status.upper() != "ACTIVE":
            return KycCheckResult(False, f"Company is {status.lower()} at CAC", name)
        if not business_name_matches(business_name, name):
            return KycCheckResult(
                False, f"CAC name ({name or 'not given'}) does not match the business name", name
            )
        return KycCheckResult(True, name=name)
