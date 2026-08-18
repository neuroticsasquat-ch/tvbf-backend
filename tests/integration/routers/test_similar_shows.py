"""Integration tests for `GET /shows/{id}/similar` (NEU-1053).

The read half of project spec §2: `catalog.show_recommendation` is already
mirrored by the ingest and the nightly delta (NEU-1052), so this route is plain
SQL over it — no upstream call, ADR-0002 without exception.

Three of the assertions below are the spec's decisions rather than incidental
behaviour:

* **Twenty are stored and twelve are served**, and the cap is applied *after*
  the filters — which is what the eight rows of headroom exist for.
* **`adult` and `deleted_upstream_at` are filtered at read time**, on NEU-1108's
  precedent: a list mirrored in March can name a show tombstoned in June, and a
  write-time copy of the filter would leave a resurrected show permanently
  invisible.
* **A show with no rows answers `200 []`**, so the SPA tells "no section" from
  "the request failed" by status code — while an unknown show still 404s, for
  the reason `/shows/{id}/cast` gives: an empty result cannot stand in for a
  missing show when empty is ordinary (8% of the long tail).

NEU-1185 added the viewer's two fields — `in_my_shows` and `my_rating` — and with
them the `no-store` header, which is the trade rather than a side effect: the
route gave up a body byte-identical for every viewer to stop being the one grid
in the app that knows you track a show and declines to say so.
"""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from tests.fixtures.browse.seed import seed
from tvbf.app import tokens
from tvbf.app.repos import session_repo, show_membership_repo, show_rating_repo
from tvbf.catalog import models as m
from tvbf.main import app

# Ids well clear of the browse seed's 1..10.
_FILLER_BASE = 200
_TOMBSTONED_ID = 300
_ADULT_ID = 301


@pytest.fixture
async def client(authed_client, session):
    """Authed ASGI client with the browse seed loaded."""
    await seed(session)
    yield authed_client


@pytest.fixture
async def seeded_similar(client, session):
    """Recommendations for show 1: fourteen eligible targets, plus a tombstoned
    and an adult one seeded at ranks 2 and 3 so the filters have to remove them
    from the middle of the served window rather than off its end.

    Show 2 is left bare — a show TMDB recommends nothing for is ordinary.
    """
    session.add(
        m.Show(
            id=_TOMBSTONED_ID,
            tmdb_id=_TOMBSTONED_ID,
            name="Gone Upstream",
            deleted_upstream_at=datetime.now(tz=UTC),
        )
    )
    session.add(m.Show(id=_ADULT_ID, tmdb_id=_ADULT_ID, name="Adult Show", adult=True))
    for offset in range(14):
        show_id = _FILLER_BASE + offset
        session.add(m.Show(id=show_id, tmdb_id=show_id, name=f"Similar {offset:02d}"))
    await session.flush()

    session.add(m.ShowRecommendation(source_show_id=1, rank=2, target_show_id=_TOMBSTONED_ID))
    session.add(m.ShowRecommendation(source_show_id=1, rank=3, target_show_id=_ADULT_ID))
    # Ranks 1, 4, 5, ... — inserted last and out of order so the route is proved
    # to sort by rank rather than to echo insertion or id order.
    ranks = [1, *range(4, 17)]
    for rank, offset in zip(reversed(ranks), range(14), strict=True):
        session.add(
            m.ShowRecommendation(source_show_id=1, rank=rank, target_show_id=_FILLER_BASE + offset)
        )
    await session.commit()
    return client


async def test_returns_targets_in_tmdb_rank_order(seeded_similar):
    r = await seeded_similar.get("/shows/1/similar")
    assert r.status_code == 200
    # Rank 1 went to the last filler, 4 to the one before it, and so on.
    assert [s["name"] for s in r.json()] == [
        "Similar 13",
        "Similar 12",
        "Similar 11",
        "Similar 10",
        "Similar 09",
        "Similar 08",
        "Similar 07",
        "Similar 06",
        "Similar 05",
        "Similar 04",
        "Similar 03",
        "Similar 02",
    ]


async def test_caps_at_twelve_after_filtering(seeded_similar):
    """Twelve survivors, not twelve rows minus the two the filters removed."""
    r = await seeded_similar.get("/shows/1/similar")
    assert len(r.json()) == 12


async def test_tombstoned_and_adult_targets_never_appear(seeded_similar):
    r = await seeded_similar.get("/shows/1/similar")
    names = {s["name"] for s in r.json()}
    assert "Gone Upstream" not in names
    assert "Adult Show" not in names


async def test_entry_is_the_show_summary_shape(seeded_similar):
    r = await seeded_similar.get("/shows/1/similar")
    entry = r.json()[0]
    assert entry["id"] == _FILLER_BASE + 13
    # The keys `ShowCard` reads, so the SPA reuses it unchanged.
    for key in (
        "id",
        "name",
        "image_medium",
        "premiered",
        "rating_average",
        "my_rating",
        "in_my_shows",
    ):
        assert key in entry
    # Unchanged from NEU-1053 and not re-decided: `ShowCard` renders neither.
    assert entry["genres"] == []
    assert entry["network"] is None


async def test_show_with_no_recommendations_returns_empty_list_not_404(seeded_similar):
    r = await seeded_similar.get("/shows/2/similar")
    assert r.status_code == 200
    assert r.json() == []


async def test_unknown_show_404s(client):
    r = await client.get("/shows/999999/similar")
    assert r.status_code == 404


async def test_cache_header_is_no_store(seeded_similar):
    """The payload carries two per-user fields that the user can mutate, so the
    route takes `_SHOW_EP_CACHE` rather than the router-level cacheable header
    (NEU-1184 §3.2).

    `no-store` rather than merely `private`: any max-age lets a refetch after a
    My Shows toggle read the pre-toggle body out of the *browser* cache and
    revert the optimistic update, which is a visibly broken toggle rather than a
    staleness nuisance.
    """
    r = await seeded_similar.get("/shows/1/similar")
    assert r.headers["Cache-Control"] == "private, no-store"


async def test_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/shows/1/similar")
    assert r.status_code == 401


async def test_tracked_shows_are_marked_not_filtered(seeded_similar, session, make_user):
    """The viewer's own membership marks its row and removes nothing — and
    somebody else's membership on another row marks nothing at all, which is
    what makes the flag per-viewer rather than per-show."""
    viewer = seeded_similar.user  # type: ignore[attr-defined]
    stranger = await make_user(email="stranger@example.com")
    await show_membership_repo.add(session, user_id=viewer.id, show_id=_FILLER_BASE + 13)
    await show_membership_repo.add(session, user_id=stranger.id, show_id=_FILLER_BASE + 12)
    await session.commit()

    rows = (await seeded_similar.get("/shows/1/similar")).json()
    # Marked, never filtered: the tracked show is still in the list, in place.
    assert [r["name"] for r in rows][:2] == ["Similar 13", "Similar 12"]
    assert {r["id"]: r["in_my_shows"] for r in rows[:2]} == {
        _FILLER_BASE + 13: True,
        _FILLER_BASE + 12: False,
    }


async def test_my_rating_is_the_viewers_own(seeded_similar, session, make_user):
    """`my_rating` is filled here where NEU-1053 left it null — and it is the
    requesting viewer's, never anyone else's."""
    viewer = seeded_similar.user  # type: ignore[attr-defined]
    stranger = await make_user(email="rater@example.com")
    await show_rating_repo.upsert(
        session, user_id=viewer.id, show_id=_FILLER_BASE + 13, stars=Decimal("4.5")
    )
    await show_rating_repo.upsert(
        session, user_id=stranger.id, show_id=_FILLER_BASE + 12, stars=Decimal("1.0")
    )
    await session.commit()

    by_id = {r["id"]: r for r in (await seeded_similar.get("/shows/1/similar")).json()}
    assert by_id[_FILLER_BASE + 13]["my_rating"] == 4.5
    assert by_id[_FILLER_BASE + 12]["my_rating"] is None


async def test_two_viewers_see_the_same_list_with_their_own_marks(
    seeded_similar, session, make_user
):
    """The per-user fields differ; the shows and their order do not."""
    viewer = seeded_similar.user  # type: ignore[attr-defined]
    other = await make_user(email="other@example.com")
    await show_membership_repo.add(session, user_id=viewer.id, show_id=_FILLER_BASE + 13)
    await show_rating_repo.upsert(
        session, user_id=viewer.id, show_id=_FILLER_BASE + 13, stars=Decimal("4.5")
    )
    await show_membership_repo.add(session, user_id=other.id, show_id=_FILLER_BASE + 12)
    await show_rating_repo.upsert(
        session, user_id=other.id, show_id=_FILLER_BASE + 12, stars=Decimal("2.0")
    )
    sess_id = tokens.new_session_id()
    await session_repo.create(
        session, session_id=sess_id, user_id=other.id, ttl_days=30, user_agent=None, ip=None
    )
    await session.commit()

    mine = (await seeded_similar.get("/shows/1/similar")).json()

    # Cookie injected via a request hook rather than the jar, for the reason
    # `authed_client` gives: httpx will not send a cookie whose domain is a
    # single-label TLD like "test".
    async def _inject(request):
        request.headers["cookie"] = f"tvbf_session={sess_id}"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
        event_hooks={"request": [_inject]},
    ) as c:
        theirs = (await c.get("/shows/1/similar")).json()

    assert [r["id"] for r in mine] == [r["id"] for r in theirs]
    assert [(r["in_my_shows"], r["my_rating"]) for r in mine][:2] == [(True, 4.5), (False, None)]
    assert [(r["in_my_shows"], r["my_rating"]) for r in theirs][:2] == [(False, None), (True, 2.0)]


async def test_statement_count_does_not_move_with_the_number_of_recommendations(
    seeded_similar, session
):
    """The invariant that protects this route (NEU-1184 §4).

    Not an absolute delta — `my_rating` made "exactly one additional query"
    stop describing it — but that nothing scales with the length of the list,
    which is what an accidental per-row `.get()` would break. Counted over
    *every* statement, `app` tables included, because that is the half the
    catalog-only count deliberately does not see.
    """
    from sqlalchemy import event

    from tvbf.db import engine as app_engine

    # Show 3 gets a single recommendation against show 1's twelve.
    session.add(m.ShowRecommendation(source_show_id=3, rank=1, target_show_id=_FILLER_BASE))
    await session.commit()

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = app_engine.sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        one = await seeded_similar.get("/shows/3/similar")
        for_one = list(statements)
        statements.clear()
        twelve = await seeded_similar.get("/shows/1/similar")
        for_twelve = list(statements)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(one.json()) == 1
    assert len(twelve.json()) == 12
    assert len(for_one) == len(for_twelve), (for_one, for_twelve)


async def test_serves_the_list_in_one_query_plus_the_existence_check(seeded_similar):
    """The list is one join, not a lookup per recommended show.

    Counted over statements touching `catalog`, like the `GET /shows` fixed-query
    test. **Two, not one**: the ticket's "one query" criterion is about the list,
    and the second is the PK lookup `/shows/{id}/cast` spends for the same reason
    — an empty result cannot stand in for a missing show once empty is ordinary.
    Read the number as one query per question asked.

    The two `app` queries NEU-1185 added — the mark and the viewer's ratings —
    are outside that number by the same counting rule `GET /shows` uses, and are
    one round trip each rather than one per row. **The invariant that protects
    this route is that nothing moves with the length of the list**, which is what
    `test_statement_count_does_not_move_with_the_number_of_recommendations`
    asserts over every statement, catalog or not.
    """
    from sqlalchemy import event

    from tvbf.db import engine as app_engine

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = app_engine.sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        r = await seeded_similar.get("/shows/1/similar")
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    catalog_queries = [s for s in statements if "catalog." in s]
    assert len(r.json()) == 12
    assert len(catalog_queries) == 2, catalog_queries
