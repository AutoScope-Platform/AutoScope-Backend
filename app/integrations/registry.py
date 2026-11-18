"""Picks the right driver for each integration from settings.

Services call `get_sms()`, `get_payments()` and friends; they never construct a
driver themselves. Tests can swap a driver with `override(...)`.

To add a vendor (e.g. a KYC provider): implement the interface from `base.py`,
add the vendor name to the matching `*_driver` setting in `core/config.py`, and add
a branch below.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.core.config import get_settings
from app.integrations import mock
from app.integrations.base import (
    EmailProvider,
    KycProvider,
    PaymentGateway,
    PushProvider,
    SmsProvider,
    SocialVerifier,
    StorageProvider,
    VinDecoder,
)

_instances: dict[str, Any] = {}


def _cached(name: str, factory: Callable[[], Any]) -> Any:
    if name not in _instances:
        _instances[name] = factory()
    return _instances[name]


def override(name: str, instance: Any | None) -> None:
    """Replace (or with None, reset) a driver. Names: sms, email, push, payments, kyc, storage, vin, social."""
    if instance is None:
        _instances.pop(name, None)
    else:
        _instances[name] = instance


def reset() -> None:
    """Forget every cached driver (tests call this when settings change)."""
    _instances.clear()


def get_sms() -> SmsProvider:
    def build() -> SmsProvider:
        s = get_settings()
        if s.sms_driver == "termii":
            from app.integrations.messaging import TermiiSms

            return TermiiSms(s)
        return mock.MockSms()

    return _cached("sms", build)


def get_email() -> EmailProvider:
    def build() -> EmailProvider:
        s = get_settings()
        if s.email_driver == "smtp":
            from app.integrations.messaging import SmtpEmail

            return SmtpEmail(s)
        return mock.MockEmail()

    return _cached("email", build)


def get_push() -> PushProvider:
    def build() -> PushProvider:
        s = get_settings()
        if s.push_driver == "fcm":
            from app.integrations.fcm import FcmPush

            return FcmPush(s)
        return mock.MockPush()

    return _cached("push", build)


def get_payments() -> PaymentGateway:
    def build() -> PaymentGateway:
        s = get_settings()
        if s.payment_driver == "paystack":
            from app.integrations.paystack import PaystackGateway

            return PaystackGateway(s.paystack_secret_key)
        return mock.MockPaymentGateway()

    return _cached("payments", build)


def get_kyc() -> KycProvider:
    # Only the mock exists until a vendor (Smile ID, Dojah...) is chosen. See docs/INTEGRATIONS.md.
    return _cached("kyc", mock.MockKyc)


def get_storage() -> StorageProvider:
    def build() -> StorageProvider:
        s = get_settings()
        if s.storage_driver == "s3":
            from app.integrations.storage import S3Storage

            return S3Storage(s)
        if s.storage_driver == "azure":
            from app.integrations.storage import AzureBlobStorage

            return AzureBlobStorage(s)
        from app.integrations.storage import LocalStorage

        return LocalStorage(s.local_storage_dir, s.jwt_secret)

    return _cached("storage", build)


def get_social() -> SocialVerifier:
    def build() -> SocialVerifier:
        s = get_settings()
        if s.social_driver == "live":
            from app.integrations.social import LiveSocialVerifier

            return LiveSocialVerifier(s)
        return mock.MockSocial()

    return _cached("social", build)


def get_vin() -> VinDecoder:
    def build() -> VinDecoder:
        if get_settings().vin_driver == "nhtsa":
            from app.integrations.vin import NhtsaVinDecoder

            return NhtsaVinDecoder()
        return mock.MockVinDecoder()

    return _cached("vin", build)
