"""Fake drivers used in local development, tests and the staging demo.

They behave like the real services closely enough to exercise every flow:
messages are recorded in an in-memory outbox (tests read it), the mock payment
gateway "charges" instantly, and the mock KYC check passes unless the uploaded
file name asks for a failure. They are refused in production by `config.py`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.integrations.base import (
    KycCheckResult,
    PaymentInit,
    PaymentVerification,
    VinInfo,
    WebhookEvent,
)

MOCK_WEBHOOK_SECRET = "mock-webhook-secret"


@dataclass
class SentMessage:
    channel: str  # "sms" | "email" | "push"
    to: str
    subject: str | None
    body: str


# Everything the mock drivers "sent". Tests inspect and clear this list.
OUTBOX: list[SentMessage] = []


class MockSms:
    def send(self, to: str, message: str) -> None:
        OUTBOX.append(SentMessage("sms", to, None, message))


class MockEmail:
    def send(self, to: str, subject: str, body: str, html: str | None = None) -> None:
        OUTBOX.append(SentMessage("email", to, subject, body))


class MockPush:
    def send(
        self, tokens: list[str], title: str, body: str, data: dict[str, str] | None = None
    ) -> int:
        for token in tokens:
            OUTBOX.append(SentMessage("push", token, title, body))
        return len(tokens)


class MockPaymentGateway:
    """Charges succeed immediately. Webhooks are signed with a fixed secret so the
    webhook endpoint can be exercised end to end in tests."""

    name = "mock"

    def __init__(self) -> None:
        self._paid: dict[str, int] = {}

    def initialize(
        self,
        *,
        reference: str,
        amount_minor: int,
        email: str,
        currency: str = "NGN",
        metadata: dict[str, Any] | None = None,
    ) -> PaymentInit:
        self._paid[reference] = amount_minor
        return PaymentInit(
            reference=reference,
            authorization_url=f"https://mock-gateway.local/pay/{reference}",
            access_code=f"mock_{uuid.uuid4().hex[:10]}",
        )

    def verify(self, reference: str) -> PaymentVerification:
        amount = self._paid.get(reference, 0)
        return PaymentVerification(
            reference=reference,
            status="success" if reference in self._paid else "failed",
            amount_minor=amount,
            method="card",
        )

    def refund(self, reference: str, amount_minor: int | None = None) -> str:
        return f"refund_{uuid.uuid4().hex[:12]}"

    def payout(
        self,
        *,
        reference: str,
        amount_minor: int,
        bank_code: str,
        account_number: str,
        account_name: str,
    ) -> str:
        return f"transfer_{uuid.uuid4().hex[:12]}"

    def verify_webhook(self, signature: str | None, body: bytes) -> bool:
        expected = hmac.new(MOCK_WEBHOOK_SECRET.encode(), body, hashlib.sha512).hexdigest()
        return bool(signature) and hmac.compare_digest(signature, expected)

    def parse_webhook(self, body: bytes) -> WebhookEvent:
        data = json.loads(body)
        return WebhookEvent(
            event_id=str(data["id"]),
            kind=data["event"],
            reference=data["reference"],
            amount_minor=data.get("amount"),
        )


def sign_mock_webhook(body: bytes) -> str:
    """Helper for tests and the demo script: produce a valid mock signature."""
    return hmac.new(MOCK_WEBHOOK_SECRET.encode(), body, hashlib.sha512).hexdigest()


class MockKyc:
    """Passes by default. A file name containing 'blurred' or 'address' fails, which
    lets the demo and tests show the 'flagged by ID check' path from the dashboard."""

    def check_document(
        self, *, doc_type: str, storage_key: str, filename: str, full_name: str
    ) -> KycCheckResult:
        name = filename.lower()
        if "blurred" in name:
            return KycCheckResult(False, "Licence image is blurred")
        if "address" in name:
            return KycCheckResult(False, "Address could not be verified")
        return KycCheckResult(True)


class MockVinDecoder:
    """Returns a deterministic fake vehicle so VIN lookup works offline."""

    def decode(self, vin: str) -> VinInfo | None:
        if len(vin) != 17:
            return None
        digest = int(hashlib.sha256(vin.encode()).hexdigest(), 16)
        makes = [
            ("Toyota", "Corolla"),
            ("Honda", "Accord"),
            ("Hyundai", "Santa Fe"),
            ("Mercedes-Benz", "C300"),
            ("Ford", "Explorer"),
        ]
        make, model = makes[digest % len(makes)]
        return VinInfo(
            vin=vin,
            make=make,
            model=model,
            year=2012 + digest % 13,
            body_class="Sedan",
            fuel_type="Petrol",
            engine="2.0L",
        )
