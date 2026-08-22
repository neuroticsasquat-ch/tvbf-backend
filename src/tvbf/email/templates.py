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


def render_invite_email(*, code: str, email: str, signup_url: str) -> tuple[str, str, str]:
    """Return (subject, html, text) for the admin-issued invite message."""
    subject = "You're invited to TV BingeFriend"
    safe_email = html.escape(email)
    safe_code = html.escape(code)
    safe_url = html.escape(signup_url, quote=True)
    text = (
        f"Hi,\n\n"
        f"You've been invited to TV BingeFriend. Use the link below to create\n"
        f"your account — your invite code is prefilled:\n"
        f"{signup_url}\n\n"
        f"If the prefill doesn't take, sign up at the link above and enter the\n"
        f"invite code manually:\n"
        f"  Email: {email}\n"
        f"  Invite code: {code}\n\n"
        f"Invite codes never expire and are good for one use.\n"
    )
    html_body = (
        f"<p>Hi,</p>"
        f"<p>You've been invited to TV BingeFriend. Use the link below to create "
        f"your account — your invite code is prefilled:</p>"
        f'<p><a href="{safe_url}">{safe_url}</a></p>'
        f"<p>If the prefill doesn't take, sign up at the link above and enter the "
        f"invite code manually:</p>"
        f"<ul>"
        f"<li>Email: <strong>{safe_email}</strong></li>"
        f"<li>Invite code: <strong>{safe_code}</strong></li>"
        f"</ul>"
        f"<p>Invite codes never expire and are good for one use.</p>"
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


def render_feedback_notification(
    *, from_email: str, from_display_name: str, subject: str, body: str, issue_url: str
) -> tuple[str, str, str]:
    """Return (subject, html, text) for the maintainer notification sent
    whenever a user submits feedback. Routed to FEEDBACK_NOTIFY_EMAIL."""
    subject_line = f"[Feedback] {subject}"
    safe_subject = html.escape(subject)
    safe_body = html.escape(body)
    safe_from_email = html.escape(from_email)
    safe_from_name = html.escape(from_display_name)
    safe_url = html.escape(issue_url, quote=True)
    text = (
        f"From: {from_display_name} <{from_email}>\n"
        f"Subject: {subject}\n\n"
        f"{body}\n\n"
        f"Linear issue: {issue_url}\n"
    )
    html_body = (
        f"<p><strong>From:</strong> {safe_from_name} &lt;{safe_from_email}&gt;</p>"
        f"<p><strong>Subject:</strong> {safe_subject}</p>"
        f'<pre style="white-space: pre-wrap; font-family: inherit;">{safe_body}</pre>'
        f'<p><a href="{safe_url}">Open in Linear →</a></p>'
    )
    return subject_line, html_body, text


def render_contact_notification(*, name: str, email: str, message: str) -> tuple[str, str, str]:
    """Return (subject, html, text) for the contact-form notification.
    Routed to FEEDBACK_NOTIFY_EMAIL."""
    subject_line = f"[Contact] message from {name}"
    safe_name = html.escape(name)
    safe_email = html.escape(email)
    safe_message = html.escape(message)
    text = f"From: {name} <{email}>\n\n{message}\n\n---\nSent from the contact form"
    html_body = (
        f"<p><strong>From:</strong> {safe_name} &lt;{safe_email}&gt;</p>"
        f'<pre style="white-space: pre-wrap; font-family: inherit;">{safe_message}</pre>'
        f"<hr>"
        f"<p>Sent from the contact form</p>"
    )
    return subject_line, html_body, text


def render_report_notification(
    *,
    reporter_email: str,
    reporter_display_name: str,
    reported_user_id: str,
    reported_display_name: str,
    reason: str,
    issue_url: str | None,
) -> tuple[str, str, str]:
    """Return (subject, html, text) for the maintainer notification sent
    whenever a user reports another user. Routed to FEEDBACK_NOTIFY_EMAIL,
    which means "the maintainer's mailbox" (NEU-1162 §8.2).

    The **subject carries the reported user's id**, not their display name:
    display names are attacker-authored, and a report subject is read under
    time pressure. The name appears in the body, escaped, as context.

    `issue_url` is None when no Linear client is configured or the issue could
    not be filed — this email is then the only notification there is, which is
    why it is sent either way.
    """
    subject_line = f"[Report] user {reported_user_id}"
    safe_reason = html.escape(reason)
    safe_reporter_email = html.escape(reporter_email)
    safe_reporter_name = html.escape(reporter_display_name)
    safe_reported_name = html.escape(reported_display_name)
    safe_reported_id = html.escape(reported_user_id)
    issue_line = f"Linear issue: {issue_url}\n" if issue_url else "Linear issue: not filed\n"
    text = (
        f"Reporter: {reporter_display_name} <{reporter_email}>\n"
        f"Reported user: {reported_display_name} (id {reported_user_id})\n\n"
        f"{reason}\n\n"
        f"{issue_line}"
    )
    if issue_url:
        safe_url = html.escape(issue_url, quote=True)
        issue_html = f'<p><a href="{safe_url}">Open in Linear \u2192</a></p>'
    else:
        issue_html = "<p><strong>Linear issue:</strong> not filed</p>"
    html_body = (
        f"<p><strong>Reporter:</strong> {safe_reporter_name} "
        f"&lt;{safe_reporter_email}&gt;</p>"
        f"<p><strong>Reported user:</strong> {safe_reported_name} "
        f"(id <code>{safe_reported_id}</code>)</p>"
        f'<pre style="white-space: pre-wrap; font-family: inherit;">{safe_reason}</pre>'
        f"{issue_html}"
    )
    return subject_line, html_body, text
