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


def render_password_reset_email(*, display_name: str, reset_url: str) -> tuple[str, str, str]:
    """Return (subject, html, text) for the password-reset message."""
    subject = "Reset your TV BingeFriend password"
    safe_name = html.escape(display_name)
    safe_url = html.escape(reset_url, quote=True)
    text = (
        f"Hi {display_name},\n\n"
        "Click the link below to set a new password for your TV BingeFriend account:\n"
        f"{reset_url}\n\n"
        "This link expires in 1 hour. If you didn't request a password reset,\n"
        "you can ignore this message — your password will stay the same.\n"
    )
    html_body = (
        f"<p>Hi {safe_name},</p>"
        "<p>Click the link below to set a new password for your TV BingeFriend account:</p>"
        f'<p><a href="{safe_url}">{safe_url}</a></p>'
        "<p>This link expires in 1 hour. If you didn't request a password reset, "
        "you can ignore this message — your password will stay the same.</p>"
    )
    return subject, html_body, text


def render_email_change_email(
    *, display_name: str, new_email: str, confirm_url: str
) -> tuple[str, str, str]:
    """Return (subject, html, text) for the confirm-email-change message,
    delivered to the **new** address."""
    subject = "Confirm your new TV BingeFriend email"
    safe_name = html.escape(display_name)
    safe_new = html.escape(new_email)
    safe_url = html.escape(confirm_url, quote=True)
    text = (
        f"Hi {display_name},\n\n"
        f"Click the link below to confirm {new_email} as your new TV BingeFriend email:\n"
        f"{confirm_url}\n\n"
        "This link expires in 24 hours. If you didn't request this change,\n"
        "you can ignore this message and your email will stay the same.\n"
    )
    html_body = (
        f"<p>Hi {safe_name},</p>"
        f"<p>Click the link below to confirm <strong>{safe_new}</strong> as your new "
        "TV BingeFriend email:</p>"
        f'<p><a href="{safe_url}">{safe_url}</a></p>'
        "<p>This link expires in 24 hours. If you didn't request this change, "
        "you can ignore this message and your email will stay the same.</p>"
    )
    return subject, html_body, text
