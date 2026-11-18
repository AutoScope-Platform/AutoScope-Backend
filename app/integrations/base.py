"""Interfaces for every external service AutoScope talks to.

The rest of the code depends only on these small interfaces, never on a vendor.
Each interface has a `mock` implementation (for local development, tests and the
staging demo) and, where a vendor is chosen, a real one. To add a vendor: write a
class with the same methods, then register it in `registry.py`.

See `docs/INTEGRATIONS.md` for the credentials each real driver needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class IntegrationError(Exception):
    """A provider call failed. Services translate this into a user-facing error."""


# --- Messaging ---------------------------------------------------------------------
class SmsProvider(Protocol):
    def send(self, to: str, message: str) -> None:
        """Send a text message to an E.164 phone number (e.g. +2348012345678)."""


class EmailProvider(Protocol):
    def send(self, to: str, subject: str, body: str, html: str | None = None) -> None: ...


class PushProvider(Protocol):
    def send(
        self, tokens: list[str], title: str, body: str, data: dict[str, str] | None = None
    ) -> int:
        """Send a push notification to device tokens. Returns how many were accepted."""


# --- Payments ----------------------------------------------------------------------
@dataclass
class PaymentInit:
    """What the app needs to open the gateway's hosted checkout (no card data touches us)."""

    reference: str
    authorization_url: str
    access_code: str | None = None


@dataclass
class PaymentVerification:
    reference: str
    status: str  # "success" | "failed" | "pending"
    amount_minor: int
    method: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class WebhookEvent:
    """A gateway callback, normalised so the payments module does not know the vendor."""

    event_id: str
    kind: str  # "charge.success" | "charge.failed" | "refund.processed" | "transfer.success" ...
    reference: str
    amount_minor: int | None = None


class PaymentGateway(Protocol):
    name: str

    def initialize(
        self,
        *,
        reference: str,
        amount_minor: int,
        email: str,
        currency: str = "NGN",
        metadata: dict[str, Any] | None = None,
    ) -> PaymentInit: ...

    def verify(self, reference: str) -> PaymentVerification: ...

    def refund(self, reference: str, amount_minor: int | None = None) -> str:
        """Refund a payment (fully if `amount_minor` is None). Returns the refund reference."""

    def payout(
        self,
        *,
        reference: str,
        amount_minor: int,
        bank_code: str,
        account_number: str,
        account_name: str,
    ) -> str:
        """Send money to a provider's bank account. Returns the transfer reference."""

    def verify_webhook(self, signature: str | None, body: bytes) -> bool: ...

    def parse_webhook(self, body: bytes) -> WebhookEvent: ...


# --- KYC ---------------------------------------------------------------------------
@dataclass
class KycCheckResult:
    passed: bool
    reason: str | None = None  # shown to admins, e.g. "Licence image is blurred"


class KycProvider(Protocol):
    def check_document(
        self, *, doc_type: str, storage_key: str, filename: str, full_name: str
    ) -> KycCheckResult:
        """Automated ID check run before a human reviews the submission (FR-13.1)."""


# --- Social sign-in (Google / Apple) ------------------------------------------------
class SocialTokenError(IntegrationError):
    """The ID token is forged, expired, for another app, or otherwise unusable."""


@dataclass
class SocialIdentity:
    """Who the provider says signed in. Only built from a token whose signature was checked."""

    provider: str  # "google" | "apple"
    subject: str  # the provider's stable user id (`sub`)
    email: str | None = None
    email_verified: bool = False
    name: str | None = None


class SocialVerifier(Protocol):
    def verify(self, provider: str, id_token: str, nonce: str | None = None) -> SocialIdentity:
        """Check the token's signature, issuer, audience and expiry (and nonce if given).

        Raises `SocialTokenError` for a bad token and `IntegrationError` when the provider
        cannot be reached or sign-in with it is not configured.
        """


# --- Storage and lookups -------------------------------------------------------------
class StorageProvider(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def get(self, key: str) -> bytes: ...

    def url(self, key: str, expires_seconds: int = 900) -> str:
        """A time-limited URL clients can use to display or download the object."""

    def delete(self, key: str) -> None: ...


@dataclass
class VinInfo:
    vin: str
    make: str | None = None
    model: str | None = None
    year: int | None = None
    body_class: str | None = None
    fuel_type: str | None = None
    engine: str | None = None


class VinDecoder(Protocol):
    def decode(self, vin: str) -> VinInfo | None:
        """Return vehicle specs for a VIN, or None if it cannot be decoded."""
