"""Integration tests for `GET /trending` (NEU-1056).

The read half of project spec §3. `catalog.trending_show` holds one snapshot
that the daily job replaces whole (NEU-1055), so this route is plain SQL over
it — no upstream call, ADR-0002 without exception.

Four of the assertions below are the spec's decisions rather than incidental
behaviour:

* **The seven-day cutoff is the server's rule and nobody else's.** A snapshot
  older than seven days serves an empty list, because the failure mode this
  route exists to prevent is week-old rows under a label reading "trending right
  now" — and a rule enforced in two places drifts.
* **`captured_at` describes the list in hand**, so it is null exactly when the
  list is empty. A stale snapshot's timestamp is not reported, or the SPA is
  handed the ingredient for re-deriving the cutoff it must not own.
* **Shows the viewer already tracks are marked, never filtered.** Trending is a
  claim about the world and seeing your own show in it is a feature.
* **`adult` and `deleted_upstream_at` are filtered at read time**, on NEU-1053's
  and NEU-1108's precedent: a snapshot taken this morning can name a show
  tombstoned this afternoon.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport

from tests.fixtures.browse.seed import seed
from tvbf.app.repos import show_membership_repo
from tvbf.catalog import models as m
from tvbf.main import app

# Ids well clear of the browse seed's 1..10.
_TOMBSTONED_ID = 300
_ADULT_ID = 301

_SIX_DAYS_AGO = datetime.now(tz=UTC) - timedelta(days=6)
_EIGHT_DAYS_AGO = datetime.now(tz=UTC) - timedelta(days=8)


@pytest.fixture
async def client(authed_client, session):
    """Authed ASGI client with the browse seed loaded."""
    await seed(session)
    yield authed_client


async def _snapshot(session, captured_at: datetime) -> None:
    """One snapshot: three seeded shows plus a tombstoned and an adult one, the
    latter two at ranks 2 and 3 so the filters have to remove them from the
    middle of the list rather than off its end. Ranks are deliberately gapped —
    the job drops an unmirrored entry rather than renumbering (NEU-1055)."""
    session.add(
        m.Show(
            id=_TOMBSTONED_ID,
            tmdb_id=_TOMBSTONED_ID,
            name="Gone Upstream",
            deleted_upstream_at=datetime.now(tz=UTC),
        )
    )
    session.add(m.Show(id=_ADULT_ID, tmdb_id=_ADULT_ID, name="Adult Show", adult=True))
    await session.flush()
    ranked = [(1, 3), (2, _TOMBSTONED_ID), (3, _ADULT_ID), (5, 1), (9, 2)]
    for rank, show_id in ranked:
        session.add(m.TrendingShow(rank=rank, show_id=show_id, captured_at=captured_at))
    await session.commit()


@pytest.fixture
async def fresh(client, session):
    await _snapshot(session, _SIX_DAYS_AGO)
    return client


@pytest.fixture
async def stale(client, session):
    await _snapshot(session, _EIGHT_DAYS_AGO)
    return client


async def test_six_day_old_snapshot_serves_the_whole_list(fresh):
    r = await fresh.get("/trending")
    assert r.status_code == 200
    assert [s["id"] for s in r.json()["shows"]] == [3, 1, 2]


async def test_eight_day_old_snapshot_serves_nothing(stale):
    """The cutoff, which is the whole reason this route exists rather than the
    SPA reading the table's shape for itself."""
    r = await stale.get("/trending")
    assert r.status_code == 200
    assert r.json()["shows"] == []


async def test_stale_snapshot_reports_no_captured_at(stale):
    """`captured_at` describes the list served. Reporting the timestamp of rows
    withheld would hand the SPA the cutoff back."""
    assert (await stale.get("/trending")).json()["captured_at"] is None


async def test_fresh_snapshot_reports_the_capture_time(fresh):
    captured = (await fresh.get("/trending")).json()["captured_at"]
    assert captured is not None
    assert abs(datetime.fromisoformat(captured) - _SIX_DAYS_AGO) < timedelta(seconds=5)


async def test_empty_table_is_an_empty_list_not_a_404(client):
    r = await client.get("/trending")
    assert r.status_code == 200
    assert r.json() == {"captured_at": None, "shows": []}


async def test_tombstoned_and_adult_entries_never_appear(fresh):
    names = {s["name"] for s in (await fresh.get("/trending")).json()["shows"]}
    assert "Gone Upstream" not in names
    assert "Adult Show" not in names


async def test_tracked_shows_are_marked_not_filtered(fresh, session, make_user):
    """The viewer's own membership marks its entry and removes nothing — and
    somebody else's membership on another entry marks nothing at all, which is
    what makes the flag per-viewer rather than per-show."""
    viewer = fresh.user  # type: ignore[attr-defined]
    stranger = await make_user(email="stranger@example.com")
    await show_membership_repo.add(session, user_id=viewer.id, show_id=1)
    await show_membership_repo.add(session, user_id=stranger.id, show_id=2)
    await session.commit()

    shows = (await fresh.get("/trending")).json()["shows"]
    assert [s["id"] for s in shows] == [3, 1, 2]
    assert {s["id"]: s["in_my_shows"] for s in shows} == {3: False, 1: True, 2: False}


async def test_entry_is_the_show_summary_shape(fresh):
    entry = (await fresh.get("/trending")).json()["shows"][0]
    # The keys `ShowCard` reads, so the SPA reuses it unchanged.
    for key in ("id", "name", "image_medium", "premiered", "rating_average", "my_rating"):
        assert key in entry


async def test_cache_header_is_no_store(fresh):
    """`in_my_shows` is a per-user field, so this list is not one a browser may
    hold on to — the same reason the show and episode routes override the
    router-level header."""
    r = await fresh.get("/trending")
    assert r.headers["Cache-Control"] == "private, no-store"


async def test_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/trending")
    assert r.status_code == 401


async def test_serves_the_list_in_a_fixed_number_of_queries(fresh):
    """The snapshot, the viewer's memberships and the viewer's ratings: three,
    and none of them moves with the length of the list."""
    from sqlalchemy import event

    from tvbf.db import engine as app_engine

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = app_engine.sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        r = await fresh.get("/trending")
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    payload_queries = [s for s in statements if "trending_show" in s or "user_show" in s]
    assert len(r.json()["shows"]) == 3
    assert len(payload_queries) == 3, payload_queries
