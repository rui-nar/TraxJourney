"""Transactional email — provider-agnostic SMTP transport (issue #113).

``EmailService`` is the abstraction the rest of the app sends through;
``get_email_service()`` picks the backend at first use (not at import time,
so a missing/misconfigured SMTP_HOST never breaks server startup):

- ``SmtpEmailService`` when SMTP_HOST is set — any transactional provider
  (Brevo, Resend, SES, Postmark, ...) works unchanged since they all expose
  an SMTP relay; switching providers is only an env-var change.
- ``ConsoleEmailService`` otherwise — logs the message instead of sending.
  This is the default until a provider account + sending domain exist.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage

import aiosmtplib

from src.utils.metrics import track_external

logger = logging.getLogger(__name__)


@dataclass
class EmailMessage:
    to: str
    subject: str
    text_body: str
    html_body: str


class EmailService(ABC):
    @abstractmethod
    async def send(self, message: EmailMessage) -> None: ...


class ConsoleEmailService(EmailService):
    """Logs the email instead of sending it. Never raises — the safe default
    for dev and for any deployment without SMTP configured.

    The body (and any html_body) is logged only when EMAIL_CONSOLE_SHOW_BODY=1
    is set — emails carry verification and invite links with bearer tokens in
    them, and logging those by default would hand out working credentials to
    anyone with log access."""

    async def send(self, message: EmailMessage) -> None:
        if os.environ.get("EMAIL_CONSOLE_SHOW_BODY") == "1":
            logger.info(
                "EMAIL (console backend, not sent) to=%s subject=%r\n%s",
                message.to, message.subject, message.text_body,
            )
        else:
            logger.info(
                "EMAIL (console backend, not sent) to=%s subject=%r",
                message.to, message.subject,
            )


class SmtpEmailService(EmailService):
    """Sends via SMTP (aiosmtplib) — works with any provider's relay."""

    def __init__(self) -> None:
        self._host = os.environ["SMTP_HOST"]
        self._port = int(os.environ.get("SMTP_PORT", "587"))
        self._username = os.environ.get("SMTP_USERNAME") or None
        self._password = os.environ.get("SMTP_PASSWORD") or None
        self._mail_from = os.environ.get("MAIL_FROM") or self._username or ""

    async def send(self, message: EmailMessage) -> None:
        mime = MimeMessage()
        mime["From"] = self._mail_from
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.text_body)
        mime.add_alternative(message.html_body, subtype="html")
        with track_external("smtp", "/send"):
            await aiosmtplib.send(
                mime,
                hostname=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                start_tls=True,
            )


_service: EmailService | None = None


def get_email_service() -> EmailService:
    """The process-wide EmailService, lazily selected on first use."""
    global _service
    if _service is None:
        if os.environ.get("SMTP_HOST"):
            _service = SmtpEmailService()
        else:
            if os.environ.get("EMAIL_CONSOLE_SHOW_BODY") == "1":
                logger.warning(
                    "SMTP_HOST is not set: emails are logged instead of sent, "
                    "and EMAIL_CONSOLE_SHOW_BODY=1 means message bodies "
                    "(including links) WILL be logged — never enable this on "
                    "a real deployment."
                )
            else:
                logger.warning(
                    "SMTP_HOST is not set: emails are logged instead of sent, "
                    "and their contents are not logged."
                )
            _service = ConsoleEmailService()
    return _service
