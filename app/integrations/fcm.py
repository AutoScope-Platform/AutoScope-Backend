"""Firebase Cloud Messaging (push) driver using the HTTP v1 API.

Needs `FCM_SERVICE_ACCOUNT_JSON`: the full contents of a Firebase service-account key
file (Firebase console -> Project settings -> Service accounts -> Generate new key).
Device tokens come from the iOS/Android apps and are registered through
`POST /me/devices` (notifications module).
"""

from __future__ import annotations

import json

import httpx

from app.core.config import Settings
from app.integrations.base import IntegrationError

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


class FcmPush:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not settings.fcm_service_account_json:
            raise IntegrationError("FCM_SERVICE_ACCOUNT_JSON is not set (see docs/INTEGRATIONS.md)")
        from google.oauth2 import service_account  # noqa: PLC0415

        info = json.loads(settings.fcm_service_account_json)
        self._project = info["project_id"]
        self._credentials = service_account.Credentials.from_service_account_info(
            info, scopes=[SCOPE]
        )
        self._client = client or httpx.Client(timeout=15)

    def _access_token(self) -> str:
        from google.auth.transport.requests import Request  # noqa: PLC0415

        if not self._credentials.valid:
            self._credentials.refresh(Request())
        return self._credentials.token

    def send(
        self, tokens: list[str], title: str, body: str, data: dict[str, str] | None = None
    ) -> int:
        url = f"https://fcm.googleapis.com/v1/projects/{self._project}/messages:send"
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        accepted = 0
        for token in tokens:
            message = {
                "message": {
                    "token": token,
                    "notification": {"title": title, "body": body},
                    "data": data or {},
                }
            }
            try:
                response = self._client.post(url, headers=headers, json=message)
            except httpx.HTTPError as exc:
                raise IntegrationError(f"FCM request failed: {exc}") from exc
            # A dead token (404/400) must not stop delivery to the user's other devices.
            if response.status_code == 200:
                accepted += 1
        return accepted
