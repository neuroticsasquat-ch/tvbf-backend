"""Provider selection + module-level `send_email` helper."""

from __future__ import annotations

from functools import lru_cache

from tvbf.config import Settings, get_settings
from tvbf.email.base import EmailClient
from tvbf.email.resend import ResendEmailClient
from tvbf.email.smtp import SmtpEmailClient


def build_email_client(settings: Settings) -> EmailClient:
    provider = settings.email_provider.lower()
    # Blank means unset, as for the other optional settings: an empty
    # `EMAIL_REPLY_TO_ADDRESS=` must not become an empty Reply-To (NEU-1537).
    default_reply_to = settings.email_reply_to_address or None
    if provider == "resend":
        if not settings.resend_api_key:
            raise ValueError("EMAIL_PROVIDER=resend requires RESEND_API_KEY")
        return ResendEmailClient(
            api_key=settings.resend_api_key,
            from_address=settings.email_from_address,
            default_reply_to=default_reply_to,
        )
    if provider == "smtp":
        return SmtpEmailClient(
            host=settings.smtp_host,
            port=settings.smtp_port,
            from_address=settings.email_from_address,
            default_reply_to=default_reply_to,
        )
    raise ValueError(f"unknown EMAIL_PROVIDER: {settings.email_provider}")


@lru_cache
def get_email_client() -> EmailClient:
    return build_email_client(get_settings())


async def send_email(
    *, to: str, subject: str, html: str, text: str, reply_to: str | None = None
) -> None:
    """Module-level convenience. Raises EmailSendError on transport failure."""
    client = get_email_client()
    await client.send(to=to, subject=subject, html=html, text=text, reply_to=reply_to)
