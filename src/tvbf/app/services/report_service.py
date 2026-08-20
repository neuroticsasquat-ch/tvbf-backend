"""Persist a user's report of another user, then notify the maintainer
(NEU-1162 §8).

**Persist, commit, then notify — the commit boundary is the contract, not an
implementation detail.** `feedback_service` persists nothing, so its failure
modes can be honest: 503 when the flag is off, 502 on a `LinearError`. A report
*does* persist, so mirroring that shape would either lie ("stored, but here is a
502") or lose the report because a third party was down. Note the sharp edge
that makes the second one real: `linear_feedback_enabled` defaults to `False`,
so a mirrored contract would 503 every report in local dev and CI.

What is reused from the feedback flow is its **components** — the same
`LinearClient` for a single `issueCreate`, the same `send_email` and
`email/templates.py` for the maintainer notification. A second notification
*mechanism* (a webhook, a Slack client, a second SMTP config) is what AC 6
forbids, and this adds none.

What it declines to reuse is the **customer modelling**. `submit_feedback` runs
`customerUpsert` on the author and attaches a `customerNeedCreate` — Linear's
"this customer wants this" signal, which feeds the customer-requests view that
exists to rank product demand. A report is a complaint about a third party, not
a need expressed by the reporter, so filing it that way corrupts the signal that
view carries. No `customerUpsert`, no `customerNeedCreate`.

The notification failure is logged at **ERROR**, not the `warning`
`feedback_service` uses: Sentry is initialised in `main.py`, so an ERROR is a
real alert, and until `GET /admin/reports` exists (NEU-1197) a dropped
notification is the only way a report goes unseen.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import NotFound, SelfReportForbidden, TooManyAttempts
from tvbf.app.models import User, UserReport
from tvbf.app.repos import user_repo, user_report_repo
from tvbf.config import Settings
from tvbf.email import EmailSendError, send_email
from tvbf.email.templates import render_report_notification
from tvbf.integrations.linear import LinearClient, LinearError

log = logging.getLogger(__name__)


def _display(user: User) -> str:
    return user.display_name or user.email


def _description(*, reporter: User, reported: User, reason: str) -> str:
    return (
        f"{reason}\n\n"
        "---\n"
        f"Reported user: {_display(reported)} (id `{reported.id}`, {reported.email})\n"
        f"Reporter: {_display(reporter)} (id `{reporter.id}`, {reporter.email})"
    )


async def submit_report(
    db: AsyncSession,
    *,
    reporter: User,
    reported_user_id: UUID,
    reason: str,
    linear: LinearClient | None,
    settings: Settings,
) -> UserReport:
    """Validate, persist, commit, then notify. Returns the persisted row.

    Raises before anything is written:
        SelfReportForbidden — the reporter named themselves.
        NotFound — no account owns `reported_user_id`.
        TooManyAttempts — the reporter has spent their budget (§6).

    Never raises for a notification failure: by then the row is committed and
    the reporter has been told we received it.

    A **disabled** user can be reported, and a **blocked** one can be reported
    in either direction. Filtering either would 404 reports about exactly the
    accounts most likely to warrant one; blocking is private and hides a problem,
    reporting is the escalation path, and letting one defeat the other makes both
    weaker.
    """
    if reported_user_id == reporter.id:
        raise SelfReportForbidden()
    reported = await user_repo.get_by_id(db, reported_user_id)
    if reported is None:
        raise NotFound()

    throttle = settings.report_throttle
    since = datetime.now(UTC) - timedelta(minutes=throttle.window_minutes)
    filed = await user_report_repo.count_since(db, reporter_id=reporter.id, since=since)
    if filed >= throttle.max_attempts:
        # The window in seconds rather than a computed remainder, exactly as
        # `auth_throttle.enforce` does it: the precise answer needs the oldest
        # row in the window, and a second query to shave seconds off a rejection
        # buys nothing.
        raise TooManyAttempts(retry_after_seconds=throttle.window_minutes * 60)

    row = await user_report_repo.record(
        db,
        reporter_id=reporter.id,
        reported_user_id=reported_user_id,
        reason=reason,
    )
    await db.commit()

    await _notify(
        reporter=reporter,
        reported=reported,
        reason=reason,
        linear=linear,
        settings=settings,
        report_id=row.id,
    )
    return row


async def _notify(
    *,
    reporter: User,
    reported: User,
    reason: str,
    linear: LinearClient | None,
    settings: Settings,
    report_id: UUID,
) -> None:
    """Best-effort Linear issue + maintainer email. Both failures are logged and
    neither is raised — the row is already committed."""
    issue_url: str | None = None
    if linear is not None and settings.linear_team_id:
        label_ids = [settings.linear_report_label_id] if settings.linear_report_label_id else None
        try:
            issue = await linear.issue_create(
                team_id=settings.linear_team_id,
                # The **id**, never the display name: display names are
                # attacker-authored and a report title is read under time
                # pressure. The name is in the body, where it is context rather
                # than structure.
                title=f"User report: {reported.id}",
                description=_description(reporter=reporter, reported=reported, reason=reason),
                label_ids=label_ids,
            )
        except LinearError:
            log.error(
                "report.linear_issue_failed report_id=%s reported_user_id=%s",
                report_id,
                reported.id,
                exc_info=True,
            )
        else:
            issue_url = issue["url"]
    else:
        # Not an ERROR: Linear being unconfigured is the *configured* state in
        # local dev and CI (`linear_feedback_enabled` defaults to False), so
        # alerting on it would train the alert to be ignored. The maintainer
        # email below still goes out, carrying "not filed".
        log.warning(
            "report.linear_not_configured report_id=%s reported_user_id=%s",
            report_id,
            reported.id,
        )

    if not settings.feedback_notify_email:
        return
    subject, html_body, text_body = render_report_notification(
        reporter_email=reporter.email,
        reporter_display_name=_display(reporter),
        reported_user_id=str(reported.id),
        reported_display_name=_display(reported),
        reason=reason,
        issue_url=issue_url,
    )
    try:
        await send_email(
            to=settings.feedback_notify_email,
            subject=subject,
            html=html_body,
            text=text_body,
        )
    except EmailSendError:
        log.error(
            "report.notification_send_failed report_id=%s reported_user_id=%s",
            report_id,
            reported.id,
            exc_info=True,
        )
