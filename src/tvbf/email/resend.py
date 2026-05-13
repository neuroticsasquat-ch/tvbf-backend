"""Resend HTTP API client. Used in production."""

from __future__ import annotations

import logging

import httpx

from tvbf.email.base import EmailSendError

log = logging.getLogger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"


class ResendEmailClient:
    """Thin async wrapper around Resend's /emails endpoint."""

    def __init__(self, *, api_key: str, from_address: str, timeout_seconds: float = 10.0) -> None:
        self._api_key = api_key
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
        payload = {
            "from": self._from,
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    _RESEND_API_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.HTTPError as e:
            log.warning("resend.transport_error", exc_info=True)
            raise EmailSendError(f"resend transport error: {e}") from e

        if resp.status_code >= 400:
            log.warning("resend.http_error status=%s body=%s", resp.status_code, resp.text[:500])
            raise EmailSendError(f"resend returned HTTP {resp.status_code}: {resp.text[:200]}")
