"""Hand-rolled HTML+text bodies for transactional emails.

Kept as plain Python functions for v1 — when we add more email types we can
move to a real template engine. Display names are html-escaped because they're
user input.
"""

from __future__ import annotations

import html


def render_verification_email(*, display_name: str, verify_url: str) -> tuple[str, str, str]:
    """Return (subject, html, text) for the email-verification message."""
    subject = "Verify your TV BingeFriend email"
    safe_name = html.escape(display_name)
    safe_url = html.escape(verify_url, quote=True)
    text = (
        f"Hi {display_name},\n\n"
        "Click the link below to verify your email address:\n"
        f"{verify_url}\n\n"
        "This link expires in 24 hours. If you didn't sign up for TV BingeFriend,\n"
        "you can ignore this message.\n"
    )
    html_body = (
        f"<p>Hi {safe_name},</p>"
        "<p>Click the link below to verify your email address:</p>"
        f'<p><a href="{safe_url}">{safe_url}</a></p>'
        "<p>This link expires in 24 hours. If you didn't sign up for TV BingeFriend, "
        "you can ignore this message.</p>"
    )
    return subject, html_body, text
