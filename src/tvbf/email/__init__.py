"""Provider-agnostic email sending.

Production uses Resend (HTTP API); local dev uses the Mailpit container
over plain SMTP. Selection is driven by `EMAIL_PROVIDER` in `tvbf.config`.

Callers should `from tvbf.email import send_email` and `await` it. Send
failures raise `EmailSendError` so callers can log + continue without
500ing the user request that triggered the send.
"""

from tvbf.email.base import EmailClient, EmailSendError
from tvbf.email.factory import get_email_client, send_email

__all__ = ["EmailClient", "EmailSendError", "get_email_client", "send_email"]
