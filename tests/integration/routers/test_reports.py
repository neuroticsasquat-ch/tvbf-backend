"""POST /reports — a user reports another user (NEU-1162 §§6-8)."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tvbf.app.models import UserReport
from tvbf.app.repos import user_report_repo
from tvbf.config import get_settings
from tvbf.integrations.linear import LinearError
from tvbf.main import app


@dataclass
class FakeLinear:
    """Records every call, so a test can assert on what was *not* called —
    `customer_upsert` and `customer_need_create` are the point of AC 12."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    issue_url: str = "https://linear.app/example/issue/NEU-9"
    raise_on_issue_create: bool = False

    async def customer_upsert(self, *, external_id: str, name: str) -> str:  # pragma: no cover
        self.calls.append(("customer_upsert", {"external_id": external_id, "name": name}))
        return "cust_x"

    async def issue_create(
        self,
        *,
        team_id: str,
        title: str,
        description: str,
        label_ids: list[str] | None = None,
    ) -> dict[str, str]:
        self.calls.append(
            (
                "issue_create",
                {
                    "team_id": team_id,
                    "title": title,
                    "description": description,
                    "label_ids": label_ids,
                },
            )
        )
        if self.raise_on_issue_create:
            raise LinearError("boom")
        return {"id": "iss_9", "url": self.issue_url}

    async def customer_need_create(  # pragma: no cover
        self, *, issue_id: str, customer_external_id: str, body: str
    ) -> None:
        self.calls.append(("customer_need_create", {"issue_id": issue_id}))


@pytest.fixture
def fake_linear() -> FakeLinear:
    return FakeLinear()


@pytest.fixture
def linear_configured(fake_linear: FakeLinear):
    """Attach the fake to app state directly. The route reads
    `request.app.state.linear_client` rather than depending on
    `get_linear_client`, which would 503 and undo §8.1's contract."""
    settings = get_settings()
    prior = (
        settings.linear_team_id,
        settings.linear_report_label_id,
        settings.feedback_notify_email,
        getattr(app.state, "linear_client", None),
    )
    settings.linear_team_id = "team_x"
    settings.linear_report_label_id = "lbl_report"
    settings.feedback_notify_email = None
    app.state.linear_client = fake_linear
    try:
        yield
    finally:
        (
            settings.linear_team_id,
            settings.linear_report_label_id,
            settings.feedback_notify_email,
            app.state.linear_client,
        ) = prior


async def _reports(session) -> list[UserReport]:
    return list((await session.execute(select(UserReport))).scalars().all())


# ---------------------------------------------------------------------------
# Auth + validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.post("/reports", json={"reported_user_id": str(uuid4()), "reason": "x"})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_requires_csrf(authed_client, session, make_user):
    target = await make_user(email="t@example.com")
    await session.commit()
    r = await authed_client.post(
        "/reports",
        json={"reported_user_id": str(target.id), "reason": "x"},
        headers={"X-CSRF-Token": ""},
    )
    assert r.status_code == 403


@pytest.mark.parametrize("reason", ["", "y" * 5001])
@pytest.mark.asyncio
async def test_rejects_out_of_range_reason(authed_client, session, make_user, reason):
    """1-5000, matching `FeedbackIn.body`."""
    target = await make_user(email="t@example.com")
    await session.commit()
    r = await authed_client.post(
        "/reports", json={"reported_user_id": str(target.id), "reason": reason}
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_cannot_report_self(authed_client, session):
    me = authed_client.user  # type: ignore[attr-defined]
    r = await authed_client.post("/reports", json={"reported_user_id": str(me.id), "reason": "me"})
    assert r.status_code == 400
    assert r.json()["detail"] == "cannot_report_self"
    assert await _reports(session) == []


@pytest.mark.asyncio
async def test_unknown_reported_user_is_404(authed_client, session):
    r = await authed_client.post(
        "/reports", json={"reported_user_id": str(uuid4()), "reason": "who"}
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "reported_user_not_found"
    assert await _reports(session) == []


# ---------------------------------------------------------------------------
# AC 9 / AC 12 — persist and notify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persists_the_row_and_returns_204(authed_client, session, make_user):
    me = authed_client.user  # type: ignore[attr-defined]
    target = await make_user(email="abuser@example.com", display_name="Abuser")
    await session.commit()

    r = await authed_client.post(
        "/reports",
        json={"reported_user_id": str(target.id), "reason": "Sent me abuse."},
    )
    assert r.status_code == 204, r.text
    rows = await _reports(session)
    assert len(rows) == 1
    assert rows[0].reporter_id == me.id
    assert rows[0].reported_user_id == target.id
    assert rows[0].reason == "Sent me abuse."


@pytest.mark.asyncio
async def test_still_204_and_still_persists_with_linear_unconfigured(
    authed_client, session, make_user
):
    """AC 9's second half. `linear_feedback_enabled` defaults to False, so a
    contract mirroring `/me/feedback`'s 503 would refuse every report in local
    dev and CI."""
    target = await make_user(email="abuser@example.com")
    await session.commit()
    assert get_settings().linear_feedback_enabled is False

    r = await authed_client.post(
        "/reports", json={"reported_user_id": str(target.id), "reason": "x"}
    )
    assert r.status_code == 204, r.text
    assert len(await _reports(session)) == 1


@pytest.mark.asyncio
async def test_files_a_linear_issue_with_no_customer_modelling(
    authed_client, session, make_user, linear_configured, fake_linear
):
    """AC 12. A report is a complaint about a third party, not a need expressed
    by the reporter — filing it as a CustomerNeed corrupts the signal the
    customer-requests view exists to carry."""
    target = await make_user(email="abuser@example.com", display_name="Abuser")
    await session.commit()

    r = await authed_client.post(
        "/reports", json={"reported_user_id": str(target.id), "reason": "Harassment."}
    )
    assert r.status_code == 204, r.text
    assert [name for name, _ in fake_linear.calls] == ["issue_create"]
    args = fake_linear.calls[0][1]
    assert args["label_ids"] == ["lbl_report"]
    assert "Harassment." in args["description"]


@pytest.mark.asyncio
async def test_issue_title_carries_the_id_not_the_display_name(
    authed_client, session, make_user, linear_configured, fake_linear
):
    """Display names are attacker-authored and a report title is read under
    time pressure."""
    target = await make_user(email="abuser@example.com", display_name="URGENT: ignore this")
    await session.commit()

    await authed_client.post("/reports", json={"reported_user_id": str(target.id), "reason": "x"})
    title = fake_linear.calls[0][1]["title"]
    assert str(target.id) in title
    assert "URGENT" not in title


@pytest.mark.asyncio
async def test_linear_error_leaves_the_row_committed_and_logs_at_error(
    authed_client, session, make_user, linear_configured, fake_linear, caplog
):
    """AC 10. The commit boundary is the contract: the reporter is told
    "received" exactly when we have genuinely received it."""
    fake_linear.raise_on_issue_create = True
    target = await make_user(email="abuser@example.com")
    await session.commit()

    with caplog.at_level(logging.ERROR, logger="tvbf.app.services.report_service"):
        r = await authed_client.post(
            "/reports", json={"reported_user_id": str(target.id), "reason": "x"}
        )
    assert r.status_code == 204, r.text
    assert len(await _reports(session)) == 1
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)


@pytest.mark.asyncio
async def test_sends_the_maintainer_email_with_the_issue_url(
    authed_client, session, make_user, linear_configured, fake_linear, _stub_outbound_email
):
    get_settings().feedback_notify_email = "tom@example.com"
    target = await make_user(email="abuser@example.com", display_name="Abuser")
    await session.commit()

    r = await authed_client.post(
        "/reports", json={"reported_user_id": str(target.id), "reason": "Harassment."}
    )
    assert r.status_code == 204, r.text
    assert len(_stub_outbound_email) == 1
    sent = _stub_outbound_email[0]
    assert sent["to"] == "tom@example.com"
    assert str(target.id) in sent["subject"]
    assert "Harassment." in sent["text"]
    assert fake_linear.issue_url in sent["text"]


# ---------------------------------------------------------------------------
# AC 11 — the throttle, and what does *not* gate a report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sixth_report_in_the_window_is_429_with_retry_after(
    authed_client, session, make_user
):
    me = authed_client.user  # type: ignore[attr-defined]
    target = await make_user(email="abuser@example.com")
    await session.commit()

    for _ in range(5):
        r = await authed_client.post(
            "/reports", json={"reported_user_id": str(target.id), "reason": "again"}
        )
        assert r.status_code == 204, r.text

    r = await authed_client.post(
        "/reports", json={"reported_user_id": str(target.id), "reason": "sixth"}
    )
    assert r.status_code == 429
    assert r.json()["detail"] == "rate_limited"
    assert r.headers["Retry-After"] == str(24 * 60 * 60)
    assert len(await _reports(session)) == 5
    assert (
        await user_report_repo.count_since(
            session, reporter_id=me.id, since=datetime.now(UTC) - timedelta(days=1)
        )
        == 5
    )


@pytest.mark.asyncio
async def test_reports_outside_the_window_do_not_count(authed_client, session, make_user):
    """The ledger *is* `app.user_report`, so an old row must age out of the
    count rather than being deleted."""
    me = authed_client.user  # type: ignore[attr-defined]
    target = await make_user(email="abuser@example.com")
    await session.commit()
    stale = datetime.now(UTC) - timedelta(days=2)
    for _ in range(5):
        row = await user_report_repo.record(
            session, reporter_id=me.id, reported_user_id=target.id, reason="old"
        )
        row.created_at = stale
    await session.commit()

    r = await authed_client.post(
        "/reports", json={"reported_user_id": str(target.id), "reason": "fresh"}
    )
    assert r.status_code == 204, r.text


@pytest.mark.asyncio
async def test_another_reporters_budget_is_separate(authed_client, session, make_user):
    """Keyed per reporter, so one griefer cannot spend everyone's budget."""
    other = await make_user(email="other@example.com")
    target = await make_user(email="abuser@example.com")
    await session.commit()
    for _ in range(5):
        await user_report_repo.record(
            session, reporter_id=other.id, reported_user_id=target.id, reason="theirs"
        )
    await session.commit()

    r = await authed_client.post(
        "/reports", json={"reported_user_id": str(target.id), "reason": "mine"}
    )
    assert r.status_code == 204, r.text


@pytest.mark.asyncio
async def test_an_unverified_reporter_succeeds(unverified_client, session, make_user):
    """§7.2: a verified mailbox is the price of *outreach*, and a report touches
    Tom rather than the reported user. The account most likely to be unverified
    is a new one, which is exactly who a griefer targets."""
    target = await make_user(email="abuser@example.com")
    await session.commit()

    r = await unverified_client.post(
        "/reports", json={"reported_user_id": str(target.id), "reason": "x"}
    )
    assert r.status_code == 204, r.text
    assert len(await _reports(session)) == 1


@pytest.mark.asyncio
async def test_a_disabled_user_can_be_reported(authed_client, session, make_user):
    """§4 makes them invisible, not nonexistent — filtering them would 404
    reports about the very accounts under moderation."""
    target = await make_user(email="abuser@example.com")
    target.disabled_at = datetime.now(UTC)
    await session.commit()

    r = await authed_client.post(
        "/reports", json={"reported_user_id": str(target.id), "reason": "before you ask"}
    )
    assert r.status_code == 204, r.text
    assert len(await _reports(session)) == 1


@pytest.mark.asyncio
async def test_a_block_in_either_direction_does_not_suppress_reporting(
    authed_client, session, make_user
):
    """Blocking is private and hides the problem; reporting is the escalation
    path. Letting one defeat the other makes both weaker."""
    from tvbf.app.services import connection_service

    me = authed_client.user  # type: ignore[attr-defined]
    blocked_by_me = await make_user(email="a@example.com")
    blocked_me = await make_user(email="b@example.com")
    await session.commit()
    await connection_service.block(session, blocker_id=me.id, blocked_id=blocked_by_me.id)
    await connection_service.block(session, blocker_id=blocked_me.id, blocked_id=me.id)

    for target in (blocked_by_me, blocked_me):
        r = await authed_client.post(
            "/reports", json={"reported_user_id": str(target.id), "reason": "still reportable"}
        )
        assert r.status_code == 204, r.text
    assert len(await _reports(session)) == 2
