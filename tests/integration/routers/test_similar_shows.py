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
"""

from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport

from tests.fixtures.browse.seed import seed
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
    for key in ("id", "name", "image_medium", "premiered", "rating_average", "my_rating"):
        assert key in entry


async def test_show_with_no_recommendations_returns_empty_list_not_404(seeded_similar):
    r = await seeded_similar.get("/shows/2/similar")
    assert r.status_code == 200
    assert r.json() == []


async def test_unknown_show_404s(client):
    r = await client.get("/shows/999999/similar")
    assert r.status_code == 404


async def test_cache_header_is_the_browse_default(seeded_similar):
    """No per-user field in the payload, so the route keeps the router-level
    cacheable header rather than the `no-store` the show and episode routes
    need."""
    r = await seeded_similar.get("/shows/1/similar")
    assert r.headers["Cache-Control"] == "private, max-age=300"


async def test_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/shows/1/similar")
    assert r.status_code == 401


async def test_serves_the_list_in_one_query(seeded_similar):
    """The list is one join, not a lookup per recommended show.

    Counted over statements touching `catalog`, like the `GET /shows` fixed-query
    test: the existence check is the second, and it is the same PK lookup
    `/shows/{id}/cast` spends to tell a bare show from a missing one.
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
