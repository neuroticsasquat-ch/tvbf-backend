"""Route tests for POST /me/feedback."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.config import get_settings
from tvbf.deps import get_linear_client
from tvbf.integrations.linear import LinearError
from tvbf.main import app


@dataclass
class FakeLinear:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    customer_upsert_id: str = "cust_123"
    issue_create_id: str = "iss_456"
    issue_create_url: str = "https://linear.app/example/issue/NEU-1"
    raise_on: str | None = None

    async def customer_upsert(self, *, external_id: str, name: str) -> str:
        self.calls.append(("customer_upsert", {"external_id": external_id, "name": name}))
        if self.raise_on == "customer_upsert":
            raise LinearError("boom")
        return self.customer_upsert_id

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
        if self.raise_on == "issue_create":
            raise LinearError("boom")
        return {"id": self.issue_create_id, "url": self.issue_create_url}

    async def customer_need_create(
        self, *, issue_id: str, customer_external_id: str, body: str
    ) -> None:
        self.calls.append(
            (
                "customer_need_create",
                {
                    "issue_id": issue_id,
                    "customer_external_id": customer_external_id,
                    "body": body,
                },
            )
        )
        if self.raise_on == "customer_need_create":
            raise LinearError("boom")


@pytest.fixture
def fake_linear() -> FakeLinear:
    return FakeLinear()


@pytest.fixture
def feedback_enabled(fake_linear: FakeLinear):
    settings = get_settings()
    prior_enabled = settings.linear_feedback_enabled
    prior_team = settings.linear_team_id
    prior_label = settings.linear_feedback_label_id
    prior_notify = settings.feedback_notify_email
    settings.linear_feedback_enabled = True
    settings.linear_team_id = "team_x"
    settings.linear_feedback_label_id = "lbl_x"
    settings.feedback_notify_email = None
    app.dependency_overrides[get_linear_client] = lambda: fake_linear
    try:
        yield
    finally:
        settings.linear_feedback_enabled = prior_enabled
        settings.linear_team_id = prior_team
        settings.linear_feedback_label_id = prior_label
        settings.feedback_notify_email = prior_notify
        app.dependency_overrides.pop(get_linear_client, None)


@pytest.mark.asyncio
async def test_submit_feedback_happy_path(authed_client, feedback_enabled, fake_linear):
    r = await authed_client.post(
        "/me/feedback",
        json={"subject": "Bug in star rating", "body": "Clicking flashes then reverts."},
    )
    assert r.status_code == 204, r.text
    kinds = [k for k, _ in fake_linear.calls]
    assert kinds == ["customer_upsert", "issue_create", "customer_need_create"]
    upsert_args = fake_linear.calls[0][1]
    assert upsert_args["external_id"].startswith("tvbf-user-")
    issue_args = fake_linear.calls[1][1]
    assert issue_args["title"] == "Bug in star rating"
    assert "Clicking flashes then reverts." in issue_args["description"]
    assert "From:" in issue_args["description"]
    assert issue_args["label_ids"] == ["lbl_x"]
    need_args = fake_linear.calls[2][1]
    assert need_args["issue_id"] == fake_linear.issue_create_id
    assert need_args["customer_external_id"] == upsert_args["external_id"]


@pytest.mark.asyncio
async def test_submit_feedback_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.post("/me/feedback", json={"subject": "x", "body": "y"})
    # require_csrf runs before get_current_user in FastAPI's dependency
    # resolution, so an unauthenticated request without a CSRF cookie surfaces
    # as 403 rather than 401. Either rejection is acceptable; only 200/204
    # would be a bug.
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_submit_feedback_requires_csrf(authed_client, feedback_enabled):
    r = await authed_client.post(
        "/me/feedback",
        json={"subject": "x", "body": "y"},
        headers={"X-CSRF-Token": ""},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_submit_feedback_rejects_oversize_body(authed_client, feedback_enabled):
    r = await authed_client.post(
        "/me/feedback",
        json={"subject": "x", "body": "y" * 5001},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_submit_feedback_rejects_empty_subject(authed_client, feedback_enabled):
    r = await authed_client.post(
        "/me/feedback",
        json={"subject": "", "body": "y"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_submit_feedback_returns_502_on_linear_error(
    authed_client, feedback_enabled, fake_linear
):
    fake_linear.raise_on = "issue_create"
    r = await authed_client.post("/me/feedback", json={"subject": "x", "body": "y"})
    assert r.status_code == 502
    assert r.json()["detail"] == "Could not submit feedback."


@pytest.mark.asyncio
async def test_submit_feedback_disabled_returns_503(authed_client):
    settings = get_settings()
    prior = settings.linear_feedback_enabled
    settings.linear_feedback_enabled = False
    try:
        r = await authed_client.post("/me/feedback", json={"subject": "x", "body": "y"})
        assert r.status_code == 503
    finally:
        settings.linear_feedback_enabled = prior


@pytest.mark.asyncio
async def test_submit_feedback_sends_notification_email_when_configured(
    authed_client, feedback_enabled, fake_linear, _stub_outbound_email
):
    settings = get_settings()
    settings.feedback_notify_email = "tom@example.com"
    r = await authed_client.post(
        "/me/feedback",
        json={"subject": "A subject", "body": "A body"},
    )
    assert r.status_code == 204, r.text
    assert len(_stub_outbound_email) == 1
    sent = _stub_outbound_email[0]
    assert sent["to"] == "tom@example.com"
    assert sent["subject"] == "[Feedback] A subject"
    assert "A body" in sent["text"]
    assert fake_linear.issue_create_url in sent["text"]


@pytest.mark.asyncio
async def test_submit_feedback_skips_notification_when_not_configured(
    authed_client, feedback_enabled, _stub_outbound_email
):
    r = await authed_client.post(
        "/me/feedback",
        json={"subject": "A subject", "body": "A body"},
    )
    assert r.status_code == 204
    assert _stub_outbound_email == []
