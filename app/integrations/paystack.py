"""Paystack payment gateway driver.

Needs `PAYSTACK_SECRET_KEY` (use the *test* key, `sk_test_...`, until launch).
Card entry happens on Paystack's hosted page, so no card data reaches AutoScope
(PCI scope stays with Paystack, FSD 5.2).

Webhooks: in the Paystack dashboard set the webhook URL to
``https://<api-host>/api/v1/payments/webhooks/paystack``. Paystack signs the raw
body with your secret key (HMAC-SHA512) in the `x-paystack-signature` header.

Amounts are in kobo, which is exactly our integer minor unit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx

from app.integrations.base import (
    IntegrationError,
    PaymentInit,
    PaymentVerification,
    WebhookEvent,
)

BASE_URL = "https://api.paystack.co"


class PaystackGateway:
    name = "paystack"

    def __init__(self, secret_key: str | None, client: httpx.Client | None = None) -> None:
        if not secret_key:
            raise IntegrationError("PAYSTACK_SECRET_KEY is not set (see docs/INTEGRATIONS.md)")
        self._secret = secret_key
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=20)
        self._headers = {"Authorization": f"Bearer {secret_key}"}

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, headers=self._headers, **kwargs)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise IntegrationError(f"Paystack request failed: {exc}") from exc
        if not data.get("status"):
            raise IntegrationError(f"Paystack error: {data.get('message', 'unknown error')}")
        return data["data"]

    def initialize(
        self,
        *,
        reference: str,
        amount_minor: int,
        email: str,
        currency: str = "NGN",
        metadata: dict[str, Any] | None = None,
    ) -> PaymentInit:
        data = self._call(
            "POST",
            "/transaction/initialize",
            json={
                "reference": reference,
                "amount": amount_minor,
                "email": email,
                "currency": currency,
                "metadata": metadata or {},
            },
        )
        return PaymentInit(
            reference=data["reference"],
            authorization_url=data["authorization_url"],
            access_code=data.get("access_code"),
        )

    def verify(self, reference: str) -> PaymentVerification:
        data = self._call("GET", f"/transaction/verify/{reference}")
        return PaymentVerification(
            reference=reference,
            status=data["status"],
            amount_minor=int(data["amount"]),
            method=data.get("channel"),
            raw=data,
        )

    def refund(self, reference: str, amount_minor: int | None = None) -> str:
        body: dict[str, Any] = {"transaction": reference}
        if amount_minor is not None:
            body["amount"] = amount_minor
        data = self._call("POST", "/refund", json=body)
        return str(data.get("id") or data.get("reference") or reference)

    def payout(
        self,
        *,
        reference: str,
        amount_minor: int,
        bank_code: str,
        account_number: str,
        account_name: str,
    ) -> str:
        # Paystack transfers need a "recipient" first, then a transfer to that recipient.
        recipient = self._call(
            "POST",
            "/transferrecipient",
            json={
                "type": "nuban",
                "name": account_name,
                "account_number": account_number,
                "bank_code": bank_code,
                "currency": "NGN",
            },
        )
        transfer = self._call(
            "POST",
            "/transfer",
            json={
                "source": "balance",
                "amount": amount_minor,
                "recipient": recipient["recipient_code"],
                "reference": reference,
            },
        )
        return str(transfer["transfer_code"])

    def verify_webhook(self, signature: str | None, body: bytes) -> bool:
        if not signature:
            return False
        expected = hmac.new(self._secret.encode(), body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(signature, expected)

    def parse_webhook(self, body: bytes) -> WebhookEvent:
        payload = json.loads(body)
        data = payload.get("data", {})
        reference = data.get("reference") or data.get("transaction_reference") or ""
        # Paystack has no separate event id; event type + reference is unique per delivery.
        return WebhookEvent(
            event_id=f"{payload.get('event')}:{reference}:{data.get('id', '')}",
            kind=payload.get("event", ""),
            reference=reference,
            amount_minor=data.get("amount"),
        )
