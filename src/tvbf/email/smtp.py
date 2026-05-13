"""SMTP client for local dev (Mailpit). Wraps stdlib smtplib in a thread."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from tvbf.email.base import EmailClient, EmailSendError

log = logging.getLogger(__name__)


class SmtpEmailClient(EmailClient):
    """Sends via plain SMTP. No TLS, no auth — intended for Mailpit in dev."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        from_address: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._from = from_address
        self._timeout = timeout_seconds

    async def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
    ) -> None:
        msg = EmailMessage()
        msg["From"] = self._from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")

        try:
            await asyncio.to_thread(self._send_blocking, msg)
        except (OSError, smtplib.SMTPException) as e:
            log.warning("smtp.send_error", exc_info=True)
            raise EmailSendError(f"smtp send failed: {e}") from e

    def _send_blocking(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as s:
            s.send_message(msg)
