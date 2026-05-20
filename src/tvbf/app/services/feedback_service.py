"""Compose the three Linear mutations that submit a feedback issue, then
optionally email the maintainer.

Calls are sequential because customerNeedCreate depends on the issue id from
issueCreate. customerUpsert is idempotent on externalId, so repeat submissions
from the same user reuse the existing Customer.

The maintainer-notification email is a workaround for Linear's
self-notification suppression: when the API key actor is the same human you
want to alert, Linear silently drops the inbox + email notification on
creation. The notification here is best-effort — a transport failure is
logged but never bubbled up to the user, who has already seen success.
"""

from __future__ import annotations

import logging

from tvbf.app.models import User
from tvbf.config import Settings
from tvbf.email import EmailSendError, send_email
from tvbf.email.templates import render_feedback_notification
from tvbf.integrations.linear import LinearClient

log = logging.getLogger(__name__)


def _external_id(user: User) -> str:
    return f"tvbf-user-{user.id}"


def _display(user: User) -> str:
    return user.display_name or user.email


def _description(user: User, body: str) -> str:
    return f"{body}\n\n---\nFrom: {user.email} (id `{user.id}`)"


async def submit_feedback(
    *,
    user: User,
    subject: str,
    body: str,
    linear: LinearClient,
    settings: Settings,
) -> None:
    if not settings.linear_team_id:
        raise RuntimeError("linear_team_id is not configured")

    external_id = _external_id(user)
    await linear.customer_upsert(external_id=external_id, name=_display(user))
    label_ids = [settings.linear_feedback_label_id] if settings.linear_feedback_label_id else None
    issue = await linear.issue_create(
        team_id=settings.linear_team_id,
        title=subject,
        description=_description(user, body),
        label_ids=label_ids,
    )
    await linear.customer_need_create(
        issue_id=issue["id"],
        customer_external_id=external_id,
        body=body,
    )

    if settings.feedback_notify_email:
        notify_subject, notify_html, notify_text = render_feedback_notification(
            from_email=user.email,
            from_display_name=_display(user),
            subject=subject,
            body=body,
            issue_url=issue["url"],
        )
        try:
            await send_email(
                to=settings.feedback_notify_email,
                subject=notify_subject,
                html=notify_html,
                text=notify_text,
            )
        except EmailSendError:
            log.warning(
                "feedback.notification_send_failed user_id=%s issue=%s",
                user.id,
                issue["id"],
                exc_info=True,
            )
