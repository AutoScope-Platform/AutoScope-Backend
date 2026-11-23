"""Termii SMS driver: uses the account's own API address and route, and reports refusals.
Offline: httpx's MockTransport stands in for Termii."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.integrations.base import IntegrationError
from app.integrations.messaging import TermiiSms


def make_sms(handler, **overrides) -> TermiiSms:
    settings = Settings(
        sms_driver="termii",
        termii_api_key="test-key",
        termii_sender_id="AutoScope",
        **overrides,
    )
    return TermiiSms(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_sends_to_account_address_on_dnd_route():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": "ok", "message": "Successfully Sent"})

    make_sms(handler, termii_base_url="https://v4.api.termii.com/").send(
        "+2348012345678", "Your AutoScope code is 482913"
    )

    assert str(seen[0].url) == "https://v4.api.termii.com/api/sms/send"
    body = json.loads(seen[0].content)
    assert body == {
        "to": "2348012345678",
        "from": "AutoScope",
        "sms": "Your AutoScope code is 482913",
        "type": "plain",
        "channel": "dnd",
        "api_key": "test-key",
    }


def test_generic_route_can_be_chosen():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"code": "ok"})

    make_sms(handler, termii_channel="generic").send("2348012345678", "hi")
    assert seen[0]["channel"] == "generic"


def test_refusal_is_reported_with_termii_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Insufficient balance"})

    with pytest.raises(IntegrationError) as exc:
        make_sms(handler).send("2348012345678", "hi")
    assert "400" in str(exc.value) and "Insufficient balance" in str(exc.value)


def test_network_failure_is_an_integration_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(IntegrationError):
        make_sms(handler).send("2348012345678", "hi")
