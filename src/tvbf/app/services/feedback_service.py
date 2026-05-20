"""Compose the three Linear mutations that submit a feedback issue.

Calls are sequential because customerNeedCreate depends on the issue id from
issueCreate. customerUpsert is idempotent on externalId, so repeat submissions
from the same user reuse the existing Customer.
"""

from __future__ import annotations

from tvbf.app.models import User
from tvbf.config import Settings
from tvbf.integrations.linear import LinearClient


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
    issue_id = await linear.issue_create(
        team_id=settings.linear_team_id,
        title=subject,
        description=_description(user, body),
        label_ids=label_ids,
    )
    await linear.customer_need_create(
        issue_id=issue_id,
        customer_external_id=external_id,
        body=body,
    )
