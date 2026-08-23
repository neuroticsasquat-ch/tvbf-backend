"""Route tests for the recommendations contract (NEU-1112, NEU-1178).

The routes are the whole of the frontend's contract, so the properties asserted
here are contract properties rather than implementation details: an empty answer
is a 200 with an empty list and never a 204, the twelve-item cap is the server's
to apply, and the model's rank order is returned untouched.

`POST /me/recommendations/{show_id}/dismiss` is tested here rather than in a file
of its own, because NEU-1112's contract doc names this module as its test home
and because AC 1 is naturally one test that dismisses and then refetches.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tvbf.app.models import (
    MATCHED_VIA_NAME,
    SET_STATUS_FAILED,
    SET_STATUS_SUCCEEDED,
    ActivityEvent,
    UserRecommendation,
    UserRecommendationDismissal,
    UserRecommendationSet,
    UserShowRating,
    UserShowWatch,
)
from tvbf.catalog.models import (
    Genre,
    Network,
    Show,
    ShowGenre,
    ShowNetwork,
    ShowRecommendation,
    TrendingShow,
)
from tvbf.main import app
from tvbf.recommendations import exclusion

_BASE = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


async def _show(session, show_id: int, name: str, **overrides) -> Show:
    show = Show(id=show_id, name=name, **overrides)
    session.add(show)
    await session.flush()
    return show


async def _set(
    session,
    user,
    *,
    status: str = SET_STATUS_SUCCEEDED,
    generated_at: datetime | None = None,
) -> UserRecommendationSet:
    rec_set = UserRecommendationSet(
        user_id=user.id,
        payload_hash="abc123",
        prompt_version="1",
        model="deepseek-ai/DeepSeek-V4-Pro-0813",
        status=status,
        compiled_payload={"liked": []},
    )
    if generated_at is not None:
        rec_set.generated_at = generated_at
    session.add(rec_set)
    await session.flush()
    return rec_set


async def _rec(session, rec_set, *, rank: int, show: Show, reason: str | None = None) -> None:
    session.add(
        UserRecommendation(
            set_id=rec_set.id,
            rank=rank,
            show_id=show.id,
            reason=reason if reason is not None else f"Reason {rank}",
            matched_via=MATCHED_VIA_NAME,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_recommendations_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/me/recommendations")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_no_set_is_200_with_an_empty_list_never_204(authed_client):
    r = await authed_client.get("/me/recommendations")
    assert r.status_code == 200
    assert r.json() == {"recommendations": []}


@pytest.mark.asyncio
async def test_response_is_not_cached(authed_client):
    r = await authed_client.get("/me/recommendations")
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_returns_show_summary_flattened_with_its_rank(authed_client, session):
    user = authed_client.user  # type: ignore[attr-defined]
    show = await _show(
        session,
        811001,
        "Severance",
        status="Returning Series",
        original_language="en",
        first_air_date=datetime(2022, 2, 18, tzinfo=UTC).date(),
    )
    genre = Genre(id=901, tmdb_id=18, name="Drama")
    network = Network(id=902, tmdb_id=2552, name="Apple TV+")
    session.add_all([genre, network])
    await session.flush()
    session.add_all(
        [
            ShowGenre(show_id=show.id, genre_id=genre.id),
            ShowNetwork(show_id=show.id, network_id=network.id),
        ]
    )
    rec_set = await _set(session, user)
    await _rec(session, rec_set, rank=1, show=show, reason="Because it is about work.")
    await session.commit()

    r = await authed_client.get("/me/recommendations")
    assert r.status_code == 200
    (item,) = r.json()["recommendations"]
    # The one added field...
    assert item["rank"] == 1
    # ...and `reason` is written but never served: the card has one truncated
    # 10px line for it, which is not room for a sentence. It is still stored, so
    # a client cannot be allowed to start reading it off this payload.
    assert "reason" not in item
    # ...on a flattened ShowSummary, not a nested one.
    assert "show" not in item
    assert item["id"] == show.id
    assert item["name"] == "Severance"
    assert item["status"] == "Returning Series"
    assert item["language"] == "en"
    assert item["premiered"] == "2022-02-18"
    assert item["genres"] == ["Drama"]
    assert item["network"]["name"] == "Apple TV+"


@pytest.mark.asyncio
async def test_returns_the_models_order_and_never_re_sorts_it(authed_client, session):
    user = authed_client.user  # type: ignore[attr-defined]
    # Named so that any alphabetical or id-based re-sort would reorder them.
    zebra = await _show(session, 811010, "Zebra")
    alpha = await _show(session, 811011, "Alpha")
    rec_set = await _set(session, user)
    await _rec(session, rec_set, rank=1, show=zebra)
    await _rec(session, rec_set, rank=2, show=alpha)
    await session.commit()

    r = await authed_client.get("/me/recommendations")
    items = r.json()["recommendations"]
    assert [i["name"] for i in items] == ["Zebra", "Alpha"]
    assert [i["rank"] for i in items] == [1, 2]


@pytest.mark.asyncio
async def test_caps_at_twelve(authed_client, session):
    user = authed_client.user  # type: ignore[attr-defined]
    rec_set = await _set(session, user)
    for rank in range(1, 26):
        show = await _show(session, 811100 + rank, f"Show {rank:02d}")
        await _rec(session, rec_set, rank=rank, show=show)
    await session.commit()

    r = await authed_client.get("/me/recommendations")
    items = r.json()["recommendations"]
    assert len(items) == 12
    assert [i["rank"] for i in items] == list(range(1, 13))


@pytest.mark.asyncio
async def test_filtered_rows_are_dropped_before_the_cap_not_after(authed_client, session):
    """The headroom is what a tombstone spends: twelve *survivors*, not twelve rows."""
    user = authed_client.user  # type: ignore[attr-defined]
    rec_set = await _set(session, user)
    for rank in range(1, 26):
        show = await _show(
            session,
            811200 + rank,
            f"Show {rank:02d}",
            adult=(rank == 1),
            deleted_upstream_at=_BASE if rank == 2 else None,
        )
        await _rec(session, rec_set, rank=rank, show=show)
    await session.commit()

    r = await authed_client.get("/me/recommendations")
    items = r.json()["recommendations"]
    assert len(items) == 12
    assert [i["rank"] for i in items] == list(range(3, 15))


@pytest.mark.asyncio
async def test_a_newer_failed_set_leaves_last_weeks_recommendations_standing(
    authed_client, session
):
    user = authed_client.user  # type: ignore[attr-defined]
    show = await _show(session, 811300, "Last Week")
    succeeded = await _set(session, user, generated_at=_BASE - timedelta(days=7))
    await _rec(session, succeeded, rank=1, show=show)
    await _set(session, user, status=SET_STATUS_FAILED, generated_at=_BASE)
    await session.commit()

    r = await authed_client.get("/me/recommendations")
    items = r.json()["recommendations"]
    assert [i["name"] for i in items] == ["Last Week"]


@pytest.mark.asyncio
async def test_another_users_set_is_not_visible(authed_client, session, make_user):
    other = await make_user(email="other@example.com", display_name="Other")
    show = await _show(session, 811400, "Theirs")
    rec_set = await _set(session, other)
    await _rec(session, rec_set, rank=1, show=show)
    await session.commit()

    r = await authed_client.get("/me/recommendations")
    assert r.json() == {"recommendations": []}


@pytest.mark.asyncio
async def test_shows_the_viewer_has_added_are_suppressed_and_the_next_ones_promoted(
    authed_client, session
):
    """AC 1: fifteen stored rows, the first three added, twelve cards from rank 4."""
    user = authed_client.user  # type: ignore[attr-defined]
    rec_set = await _set(session, user)
    for rank in range(1, 16):
        show = await _show(session, 811500 + rank, f"Show {rank:02d}")
        await _rec(session, rec_set, rank=rank, show=show)
        if rank <= 3:
            session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()

    r = await authed_client.get("/me/recommendations")
    items = r.json()["recommendations"]
    assert [i["rank"] for i in items] == list(range(4, 16))


@pytest.mark.asyncio
async def test_fewer_than_twelve_is_a_normal_answer(authed_client, session):
    """AC 2: thirteen rows, five suppressed, eight cards and a 200. Nothing is
    backfilled from an older set to refill the grid."""
    user = authed_client.user  # type: ignore[attr-defined]
    rec_set = await _set(session, user)
    for rank in range(1, 14):
        show = await _show(session, 811600 + rank, f"Show {rank:02d}")
        await _rec(session, rec_set, rank=rank, show=show)
        if rank % 2 == 1 and rank <= 9:
            session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()

    r = await authed_client.get("/me/recommendations")
    assert r.status_code == 200
    items = r.json()["recommendations"]
    assert [i["rank"] for i in items] == [2, 4, 6, 8, 10, 11, 12, 13]


@pytest.mark.asyncio
async def test_a_record_for_every_show_is_the_same_body_as_no_set_at_all(authed_client, session):
    """AC 3: never a 204, never a 500."""
    user = authed_client.user  # type: ignore[attr-defined]
    rec_set = await _set(session, user)
    for rank in range(1, 6):
        show = await _show(session, 811700 + rank, f"Show {rank:02d}")
        await _rec(session, rec_set, rank=rank, show=show)
        session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()

    r = await authed_client.get("/me/recommendations")
    assert r.status_code == 200
    assert r.json() == {"recommendations": []}


@pytest.mark.asyncio
async def test_serves_the_list_in_a_fixed_number_of_queries(authed_client, session):
    """AC 6: the rows with their anti-join, plus `hydrate_show_refs`' pair —
    three, whatever the size of the set and however many rows are suppressed."""
    from sqlalchemy import event

    from tvbf.db import engine as app_engine

    user = authed_client.user  # type: ignore[attr-defined]
    rec_set = await _set(session, user)
    for rank in range(1, 26):
        show = await _show(session, 811800 + rank, f"Show {rank:02d}")
        await _rec(session, rec_set, rank=rank, show=show)
        if rank % 2 == 1:
            session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = app_engine.sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        r = await authed_client.get("/me/recommendations")
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    payload_queries = [
        s
        for s in statements
        if "user_recommendation" in s or "show_genre" in s or "show_network" in s
    ]
    assert len(r.json()["recommendations"]) == 12
    assert len(payload_queries) == 3, payload_queries


@pytest.mark.asyncio
async def test_nothing_surviving_costs_one_query(authed_client, session):
    """The other half of AC 6: `hydrate_show_refs` short-circuits on an empty
    list, so a fully suppressed set spends the rows query and nothing else."""
    from sqlalchemy import event

    from tvbf.db import engine as app_engine

    user = authed_client.user  # type: ignore[attr-defined]
    rec_set = await _set(session, user)
    for rank in range(1, 6):
        show = await _show(session, 811900 + rank, f"Show {rank:02d}")
        await _rec(session, rec_set, rank=rank, show=show)
        session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = app_engine.sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        r = await authed_client.get("/me/recommendations")
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    payload_queries = [
        s
        for s in statements
        if "user_recommendation" in s or "show_genre" in s or "show_network" in s
    ]
    assert r.json() == {"recommendations": []}
    assert len(payload_queries) == 1, payload_queries


# ---------------------------------------------------------------------------
# POST /me/recommendations/{show_id}/dismiss (NEU-1178)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dismiss_refuses_an_anonymous_caller(session):
    """403 rather than 401: `require_csrf` is a route-level dependency and runs
    before `get_current_user`, so a caller with neither is refused on the CSRF
    token first. Either way nothing is written."""
    show = await _show(session, 812000, "Anon")
    await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.post(f"/me/recommendations/{show.id}/dismiss")
    assert r.status_code == 403
    assert (await session.scalars(select(UserRecommendationDismissal))).all() == []


@pytest.mark.asyncio
async def test_dismiss_requires_csrf(authed_client, session):
    show = await _show(session, 812001, "No CSRF")
    await session.commit()

    r = await authed_client.post(
        f"/me/recommendations/{show.id}/dismiss",
        headers={"X-CSRF-Token": ""},
    )

    assert r.status_code == 403


@pytest.mark.asyncio
async def test_dismiss_is_204_with_no_body(authed_client, session):
    show = await _show(session, 812002, "Dismissed")
    await session.commit()

    r = await authed_client.post(f"/me/recommendations/{show.id}/dismiss")

    assert r.status_code == 204
    assert r.content == b""


@pytest.mark.asyncio
async def test_dismiss_is_idempotent_and_leaves_one_row(authed_client, session):
    """AC 2: 204 both times, `ON CONFLICT DO NOTHING`, one row."""
    user = authed_client.user  # type: ignore[attr-defined]
    show = await _show(session, 812003, "Twice")
    await session.commit()

    first = await authed_client.post(f"/me/recommendations/{show.id}/dismiss")
    second = await authed_client.post(f"/me/recommendations/{show.id}/dismiss")

    assert [first.status_code, second.status_code] == [204, 204]
    rows = (
        await session.scalars(
            select(UserRecommendationDismissal).where(
                UserRecommendationDismissal.user_id == user.id
            )
        )
    ).all()
    assert [row.show_id for row in rows] == [show.id]


@pytest.mark.asyncio
async def test_dismiss_404s_on_a_show_that_does_not_exist(authed_client):
    r = await authed_client.post("/me/recommendations/812999/dismiss")

    assert r.status_code == 404
    assert r.json() == {"detail": "not_found"}


@pytest.mark.asyncio
async def test_dismissing_a_show_never_recommended_succeeds(authed_client, session):
    """AC 7: the never-recommend list is about future passes as much as the
    current set, so the endpoint never looks at the set."""
    user = authed_client.user  # type: ignore[attr-defined]
    show = await _show(session, 812004, "Found By Search")
    await session.commit()

    r = await authed_client.post(f"/me/recommendations/{show.id}/dismiss")

    assert r.status_code == 204
    assert await exclusion.load_show_ids_never_to_recommend(session, user_id=user.id) == {show.id}


@pytest.mark.asyncio
async def test_dismissing_removes_the_card_and_promotes_the_next(authed_client, session):
    """AC 1, end to end: dismiss, refetch, the replacement appears, ranks intact."""
    user = authed_client.user  # type: ignore[attr-defined]
    rec_set = await _set(session, user)
    shows = []
    for rank in range(1, 14):
        show = await _show(session, 812100 + rank, f"Show {rank:02d}")
        await _rec(session, rec_set, rank=rank, show=show)
        shows.append(show)
    await session.commit()

    before = (await authed_client.get("/me/recommendations")).json()["recommendations"]
    assert [i["rank"] for i in before] == list(range(1, 13))

    assert (
        await authed_client.post(f"/me/recommendations/{shows[0].id}/dismiss")
    ).status_code == 204

    after = (await authed_client.get("/me/recommendations")).json()["recommendations"]
    assert [i["rank"] for i in after] == list(range(2, 14))
    assert shows[0].id not in [i["id"] for i in after]


@pytest.mark.asyncio
async def test_dismissing_writes_no_activity_event(authed_client, session):
    """AC 9: a dismissal is private. Nothing reaches the friend feed, and
    `my_shows_service.add`'s activity emit is exactly what `dismiss` omits."""
    user = authed_client.user  # type: ignore[attr-defined]
    show = await _show(session, 812005, "Quiet")
    await session.commit()

    assert (await authed_client.post(f"/me/recommendations/{show.id}/dismiss")).status_code == 204

    events = (
        await session.scalars(select(ActivityEvent).where(ActivityEvent.actor_id == user.id))
    ).all()
    ratings = (
        await session.scalars(select(UserShowRating).where(UserShowRating.user_id == user.id))
    ).all()
    memberships = (
        await session.scalars(select(UserShowWatch).where(UserShowWatch.user_id == user.id))
    ).all()
    assert events == []
    assert ratings == []
    assert memberships == []


@pytest.mark.asyncio
async def test_one_users_dismissal_never_affects_anothers_list(authed_client, session, make_user):
    """AC 8, at the surface."""
    user = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="other-dismisser@example.com", display_name="Other")
    rec_set = await _set(session, user)
    show = await _show(session, 812006, "Shared")
    await _rec(session, rec_set, rank=1, show=show)
    session.add(UserRecommendationDismissal(user_id=other.id, show_id=show.id))
    await session.commit()

    items = (await authed_client.get("/me/recommendations")).json()["recommendations"]

    assert [i["id"] for i in items] == [show.id]


@pytest.mark.asyncio
async def test_a_dismissal_costs_the_read_no_extra_queries(authed_client, session):
    """AC 13: still three — the rows with their anti-join, plus
    `hydrate_show_refs`' pair — however many rows a dismissal suppresses."""
    from sqlalchemy import event

    from tvbf.db import engine as app_engine

    user = authed_client.user  # type: ignore[attr-defined]
    rec_set = await _set(session, user)
    for rank in range(1, 26):
        show = await _show(session, 812200 + rank, f"Show {rank:02d}")
        await _rec(session, rec_set, rank=rank, show=show)
        if rank % 2 == 1:
            session.add(UserRecommendationDismissal(user_id=user.id, show_id=show.id))
    await session.commit()

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = app_engine.sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        r = await authed_client.get("/me/recommendations")
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    payload_queries = [
        s
        for s in statements
        if "user_recommendation" in s or "show_genre" in s or "show_network" in s
    ]
    assert len(r.json()["recommendations"]) == 12
    assert len(payload_queries) == 3, payload_queries


@pytest.mark.asyncio
async def test_a_dismissal_does_not_reach_any_other_surface(authed_client, session):
    """AC 12 / spec §9: dismissal is scoped to `/me/recommendations` and the
    weekly pass, and nothing else.

    Trending, most anticipated and similar shows are catalog facts rather than
    personal suggestions — "what is trending this week" is a statement about the
    world, and removing yourself from it silently is a different feature with a
    different name. Most decisively, this ticket ships **no un-dismiss**: under a
    wider rule one tap would permanently remove a show from browse-adjacent
    surfaces with no way back, so a user must still be able to *find* what they
    dismissed.

    All five surfaces are asserted rather than the two a user reaches for most,
    because "none of them consults `recommendations/exclusion.py`" is the kind of
    claim that stays true only until somebody adds the anti-join for symmetry.
    """
    dismissed = await _show(session, 812007, "Still Findable", first_air_date=_BASE.date())
    source = await _show(session, 812008, "Source", first_air_date=_BASE.date())
    upcoming = await _show(
        session, 812009, "Still Anticipated", first_air_date=_BASE.date() + timedelta(days=30)
    )
    session.add_all(
        [
            TrendingShow(rank=1, show_id=dismissed.id, captured_at=datetime.now(tz=UTC) - timedelta(hours=1)),
            ShowRecommendation(source_show_id=source.id, target_show_id=dismissed.id, rank=1),
        ]
    )
    await session.commit()
    for show_id in (dismissed.id, upcoming.id):
        assert (
            await authed_client.post(f"/me/recommendations/{show_id}/dismiss")
        ).status_code == 204

    detail = await authed_client.get(f"/shows/{dismissed.id}")
    listed = await authed_client.get("/shows", params={"search": "Still Findable"})
    trending = await authed_client.get("/trending")
    similar = await authed_client.get(f"/shows/{source.id}/similar")
    anticipated = await authed_client.get("/anticipated")

    assert detail.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [dismissed.id]
    assert dismissed.id in [item["id"] for item in trending.json()["shows"]]
    assert [item["id"] for item in similar.json()] == [dismissed.id]
    assert upcoming.id in [item["id"] for item in anticipated.json()]
