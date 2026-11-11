"""Real SMS (Termii) and email (SMTP) drivers.

Termii: set `TERMII_API_KEY` and `TERMII_SENDER_ID`. Nigerian networks require a
registered sender ID, which can take several days to approve, so apply early.

SMTP: set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` (ZeptoMail, Resend
SMTP, Mailgun and others all work).
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

import httpx

from app.core.config import Settings
from app.integrations.base import IntegrationError


class TermiiSms:
    URL = "https://api.ng.termii.com/api/sms/send"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not (settings.termii_api_key and settings.termii_sender_id):
            raise IntegrationError("TERMII_API_KEY / TERMII_SENDER_ID are not set")
        self._key = settings.termii_api_key
        self._sender = settings.termii_sender_id
        self._client = client or httpx.Client(timeout=15)

    def send(self, to: str, message: str) -> None:
        try:
            response = self._client.post(
                self.URL,
                json={
                    "to": to.lstrip("+"),
                    "from": self._sender,
                    "sms": message,
                    "type": "plain",
                    "channel": "generic",
                    "api_key": self._key,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise IntegrationError(f"Termii request failed: {exc}") from exc


class SmtpEmail:
    def __init__(self, settings: Settings) -> None:
        if not (settings.smtp_host and settings.smtp_user and settings.smtp_password):
            raise IntegrationError("SMTP settings are not set (see docs/INTEGRATIONS.md)")
        self._s = settings

    def send(self, to: str, subject: str, body: str, html: str | None = None) -> None:
        message = EmailMessage()
        message["From"] = self._s.email_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        if html:
            message.add_alternative(html, subtype="html")
        try:
            with smtplib.SMTP(self._s.smtp_host, self._s.smtp_port, timeout=15) as server:
                server.starttls()
                server.login(self._s.smtp_user, self._s.smtp_password)
                server.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise IntegrationError(f"SMTP send failed: {exc}") from exc
