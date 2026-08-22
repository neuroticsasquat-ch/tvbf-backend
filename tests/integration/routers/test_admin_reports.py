"""GET /admin/reports — the admin report queue (NEU-1197).

A read-only surface: `POST /reports` commits the row and only then notifies
best-effort, so a notification that fails leaves a report visible nowhere. This
is what makes those rows readable over HTTP.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.app.models import UserReport
from tvbf.main import app


async def _report(
    session,
    *,
    reporter,
    reported,
    reason: str = "spam",
    created_at: datetime | None = None,
) -> UserReport:
    row = UserReport(reporter_id=reporter.id, reported_user_id=reported.id, reason=reason)
    if created_at is not None:
        row.created_at = created_at
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _as_admin(client, session):
    me = client.user  # type: ignore[attr-defined]
    me.is_admin = True
    await session.commit()
    return me


# ---------------------------------------------------------------------------
# The two gates (AC 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_a_session():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/admin/reports")
    assert r.status_code == 401
    assert r.json()["detail"] == "auth_required"


@pytest.mark.asyncio
async def test_forbidden_for_non_admin(authed_client, session, make_user):
    reporter = await make_user(email="r@example.com", display_name="R")
    reported = await make_user(email="t@example.com", display_name="T")
    await _report(session, reporter=reporter, reported=reported)
    r = await authed_client.get("/admin/reports")
    assert r.status_code == 403
    assert r.json()["detail"] == "admin_required"


@pytest.mark.asyncio
async def test_admin_session_gets_200(authed_client, session):
    await _as_admin(authed_client, session)
    r = await authed_client.get("/admin/reports")
    assert r.status_code == 200
    assert r.json() == {"items": [], "page": 1, "per_page": 50, "total": 0, "total_pages": 1}


# ---------------------------------------------------------------------------
# The row (AC 2, AC 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_carries_both_parties_and_the_full_reason(authed_client, session, make_user):
    await _as_admin(authed_client, session)
    reporter = await make_user(
        email="r@example.com", display_name="Reporter", handle="reporter_one"
    )
    reported = await make_user(
        email="t@example.com", display_name="Reported", handle="reported_one"
    )
    reason = "x" * 5000
    row = await _report(session, reporter=reporter, reported=reported, reason=reason)

    r = await authed_client.get("/admin/reports")
    assert r.status_code == 200
    (item,) = r.json()["items"]
    assert item["id"] == str(row.id)
    # The handle rides beside the display name (NEU-1163 §7). NEU-1197
    # deliberately withheld `email` here, leaving `display_name` as a
    # moderator's only label — and a report queue is exactly where "two users
    # named Tom" is most expensive to get wrong.
    assert item["reporter"] == {
        "id": str(reporter.id),
        "display_name": "Reporter",
        "handle": "reporter_one",
        "disabled_at": None,
    }
    assert item["reported_user"] == {
        "id": str(reported.id),
        "display_name": "Reported",
        "handle": "reported_one",
        "disabled_at": None,
    }
    # Verbatim and untruncated (§3.1) — escaping belongs to the renderer.
    assert item["reason"] == reason
    assert item["created_at"] is not None
    # Neither party's email is carried (§3).
    assert "email" not in item["reporter"]
    assert "email" not in item["reported_user"]


@pytest.mark.asyncio
async def test_report_filed_with_linear_disabled_is_visible(authed_client, session, make_user):
    """AC 2 — the reason this route exists. `linear_feedback_enabled` is False
    by default, so this report produced no Linear issue at all; the queue is the
    only place it can be seen."""
    reported = await make_user(email="t@example.com", display_name="Reported")
    posted = await authed_client.post(
        "/reports", json={"reported_user_id": str(reported.id), "reason": "harassment"}
    )
    assert posted.status_code == 204

    await _as_admin(authed_client, session)
    r = await authed_client.get("/admin/reports")
    (item,) = r.json()["items"]
    assert item["reason"] == "harassment"
    assert item["reported_user"]["id"] == str(reported.id)


# ---------------------------------------------------------------------------
# The filter (AC 4, AC 7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_counts_repeat_reports_against_one_account(authed_client, session, make_user):
    await _as_admin(authed_client, session)
    target = await make_user(email="t@example.com", display_name="Target")
    other = await make_user(email="o@example.com", display_name="Other")
    for i in range(3):
        reporter = await make_user(email=f"r{i}@example.com", display_name=f"R{i}")
        await _report(session, reporter=reporter, reported=target, reason=f"n{i}")
    noise_reporter = await make_user(email="n@example.com", display_name="N")
    await _report(session, reporter=noise_reporter, reported=other, reason="unrelated")

    r = await authed_client.get(f"/admin/reports?reported_user_id={target.id}")
    body = r.json()
    assert body["total"] == 3
    assert {i["reported_user"]["id"] for i in body["items"]} == {str(target.id)}


@pytest.mark.asyncio
async def test_unknown_reported_user_is_an_empty_page_not_404(authed_client, session):
    """AC 7 (§5.2) — a filter is a filter, not a lookup. `total: 0` is the true
    answer to "has this account been reported?"."""
    await _as_admin(authed_client, session)
    r = await authed_client.get(f"/admin/reports?reported_user_id={uuid4()}")
    assert r.status_code == 200
    assert r.json()["total"] == 0
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_malformed_reported_user_id_is_422(authed_client, session):
    await _as_admin(authed_client, session)
    r = await authed_client.get("/admin/reports?reported_user_id=not-a-uuid")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Ordering and paging (AC 5, AC 8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_newest_first_and_paginated(authed_client, session, make_user):
    await _as_admin(authed_client, session)
    reporter = await make_user(email="r@example.com", display_name="R")
    reported = await make_user(email="t@example.com", display_name="T")
    base = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    for i in range(5):
        await _report(
            session,
            reporter=reporter,
            reported=reported,
            reason=f"n{i}",
            created_at=base + timedelta(minutes=i),
        )

    first = (await authed_client.get("/admin/reports?per_page=2")).json()
    assert [i["reason"] for i in first["items"]] == ["n4", "n3"]
    assert (first["total"], first["total_pages"], first["page"], first["per_page"]) == (5, 3, 1, 2)

    last = (await authed_client.get("/admin/reports?per_page=2&page=3")).json()
    assert [i["reason"] for i in last["items"]] == ["n0"]


@pytest.mark.asyncio
async def test_page_size_is_bounded(authed_client, session):
    await _as_admin(authed_client, session)
    assert (await authed_client.get("/admin/reports?per_page=101")).status_code == 422
    assert (await authed_client.get("/admin/reports?per_page=0")).status_code == 422
    assert (await authed_client.get("/admin/reports?page=0")).status_code == 422
    assert (await authed_client.get("/admin/reports?page=1001")).status_code == 422


@pytest.mark.asyncio
async def test_identical_timestamps_break_by_id_desc(authed_client, session, make_user):
    """AC 8 (§6) — `created_at` defaults to transaction-start time, so two
    concurrent reports can share one. Without the `id DESC` tiebreak such a pair
    can appear on both page 1 and page 2, or on neither."""
    await _as_admin(authed_client, session)
    reporter = await make_user(email="r@example.com", display_name="R")
    reported = await make_user(email="t@example.com", display_name="T")
    stamp = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    rows = [
        await _report(
            session, reporter=reporter, reported=reported, reason=f"n{i}", created_at=stamp
        )
        for i in range(4)
    ]
    expected = [str(row.id) for row in sorted(rows, key=lambda r: r.id, reverse=True)]

    page1 = (await authed_client.get("/admin/reports?per_page=2")).json()["items"]
    page2 = (await authed_client.get("/admin/reports?per_page=2&page=2")).json()["items"]
    seen = [i["id"] for i in page1] + [i["id"] for i in page2]
    assert seen == expected, "a page boundary must neither duplicate nor drop a row"


# ---------------------------------------------------------------------------
# The queue filters nothing (AC 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reports_about_and_by_a_disabled_user_are_both_listed(
    authed_client, session, make_user
):
    """AC 6 (§4). NEU-1162 §4's four invisibility predicates are about what a
    *stranger* may see; this is the one read path whose entire purpose is to see
    what strangers cannot.

    Adding `disabled_at IS NULL` to either join hides reports about precisely
    the accounts under moderation, and retroactively erases the reports that
    justified disabling a griefer.
    """
    await _as_admin(authed_client, session)
    stamp = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    disabled_target = await make_user(email="dt@example.com", display_name="Disabled Target")
    disabled_reporter = await make_user(email="dr@example.com", display_name="Disabled Reporter")
    ordinary = await make_user(email="o@example.com", display_name="Ordinary")
    disabled_target.disabled_at = stamp
    disabled_reporter.disabled_at = stamp
    await session.commit()

    await _report(session, reporter=ordinary, reported=disabled_target, reason="about-disabled")
    await _report(session, reporter=disabled_reporter, reported=ordinary, reason="by-disabled")

    body = (await authed_client.get("/admin/reports")).json()
    reasons = {i["reason"] for i in body["items"]}
    assert "about-disabled" in reasons, "a report about a disabled account must stay listed"
    assert "by-disabled" in reasons, "a report by a disabled account must stay listed"

    by_reason = {i["reason"]: i for i in body["items"]}
    # The state is a live join, not a stored flag — clearing the flag restores
    # the truth with no backfill (§1).
    assert by_reason["about-disabled"]["reported_user"]["disabled_at"] is not None
    assert by_reason["by-disabled"]["reporter"]["disabled_at"] is not None


@pytest.mark.asyncio
async def test_reports_about_an_admin_are_listed(authed_client, session, make_user):
    """§4 — an admin may disable another admin, so a queue blind to reports
    about admins would be the one hole in the moderation surface."""
    await _as_admin(authed_client, session)
    other_admin = await make_user(email="a@example.com", display_name="Other Admin")
    other_admin.is_admin = True
    reporter = await make_user(email="r@example.com", display_name="R")
    await session.commit()
    await _report(session, reporter=reporter, reported=other_admin, reason="about-admin")

    body = (await authed_client.get("/admin/reports")).json()
    assert [i["reason"] for i in body["items"]] == ["about-admin"]


# ---------------------------------------------------------------------------
# Cache header (AC 9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_is_not_stored(authed_client, session):
    """AC 9 (§7) — the payload carries `disabled_at` for both parties, mutated
    by `PATCH /admin/users/{user_id}/disabled` in the same SPA seconds earlier.
    A heuristically-cached body answers "has this been dealt with?" wrongly."""
    await _as_admin(authed_client, session)
    r = await authed_client.get("/admin/reports")
    assert r.headers["cache-control"] == "private, no-store"
