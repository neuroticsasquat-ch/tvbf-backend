from tests.fixtures.browse.seed import GENRES, seed
from tvbf.catalog import models as m
from tvbf.catalog.browse_queries import (
    get_show_episodes,
    get_show_seasons,
    get_show_with_seasons,
    hydrate_show_refs,
    list_genres,
    list_networks,
    list_shows,
)
from tvbf.catalog.schemas import ShowFilters, build_episode_out


async def test_list_genres_returns_all_in_name_order(session):
    await seed(session)
    rows = await list_genres(session)
    assert [g.name for g in rows] == sorted(GENRES)


async def test_list_networks_returns_all_in_name_order(session):
    await seed(session)
    rows = await list_networks(session)
    # Three, not two: the former web channel is an ordinary network under TMDB.
    assert [n.name for n in rows] == ["Network A", "Network B", "Web Channel X"]


async def test_get_show_with_seasons_returns_show_and_seasons(session):
    await seed(session)
    result = await get_show_with_seasons(session, 1)
    assert result is not None
    show, seasons, genres, network = result
    assert show.name == "Running Drama"
    assert {g.name for g in genres} == {"Drama", "Crime"}
    assert network is not None and network.name == "Network A"
    assert sorted(s.season_number for s in seasons) == [1, 2]


async def test_get_show_with_seasons_returns_none_for_unknown_id(session):
    await seed(session)
    assert await get_show_with_seasons(session, 99999) is None


async def test_get_show_seasons_returns_ordered_list(session):
    await seed(session)
    seasons = await get_show_seasons(session, 1)
    assert [s.season_number for s in seasons] == [1, 2]


async def test_get_show_seasons_returns_empty_for_unknown_id(session):
    await seed(session)
    assert await get_show_seasons(session, 99999) == []


async def test_get_show_episodes_returns_all_by_default(session):
    await seed(session)
    eps = await get_show_episodes(session, 1, season=None)
    assert len(eps) == 4
    assert [(e.season_number, e.episode_number) for e in eps] == [(1, 1), (1, 2), (2, 1), (2, 2)]


async def test_get_show_episodes_filters_by_season(session):
    await seed(session)
    eps = await get_show_episodes(session, 1, season=2)
    assert len(eps) == 2
    assert all(e.season_number == 2 for e in eps)


async def test_get_show_episodes_returns_specials_after_their_season(session):
    """A TV Maze special carried a null `number` (NEU-933); `catalog.episode`
    cannot, so NEU-1042's copy numbered it negative within its real season. The
    read path must still return it *after* the season's real episodes, where the
    null used to sort — see `catalog/episodes.py`."""
    await seed(session)
    session.add(m.Episode(id=9001, show_id=1, season_number=1, episode_number=-1, name="Special"))
    await session.commit()

    eps = await get_show_episodes(session, 1, season=None)
    assert [(e.season_number, e.episode_number) for e in eps] == [
        (1, 1),
        (1, 2),
        (1, -1),
        (2, 1),
        (2, 2),
    ]

    # And the invented number does not reach the payload: `EpisodeOut.number`
    # was null for these rows before the repoint and stays null after it.
    special = build_episode_out(next(e for e in eps if e.id == 9001))
    assert special.number is None


# Task 11: base pagination
async def test_list_shows_returns_all_paginated_by_name(session):
    await seed(session)
    rows, total = await list_shows(session, ShowFilters(), sort="name", page=1, per_page=100)
    assert total == 10
    assert [s.name for s in rows] == sorted(
        [
            "Ancient Show",
            "Ended Drama",
            "Multi Genre",
            "New Show",
            "Running Comedy",
            "Running Drama",
            "Running Reality",
            "Spanish Drama",
            "TBD Show",
            "Web Only",
        ]
    )


async def test_list_shows_respects_page_boundaries(session):
    await seed(session)
    rows, total = await list_shows(session, ShowFilters(), sort="name", page=1, per_page=3)
    assert total == 10
    assert len(rows) == 3
    rows2, _ = await list_shows(session, ShowFilters(), sort="name", page=2, per_page=3)
    assert [r.id for r in rows] != [r.id for r in rows2]


# Task 12: simple filters
async def test_list_shows_search_substring_case_insensitive(session):
    await seed(session)
    rows, total = await list_shows(
        session, ShowFilters(search="drama"), sort="name", page=1, per_page=100
    )
    names = {r.name for r in rows}
    assert "Running Drama" in names
    assert "Spanish Drama" in names
    assert "Running Comedy" not in names
    assert total == len(rows)


async def test_list_shows_search_tokens_match_across_punctuation(session):
    """Each whitespace-separated token must appear as a substring of the name.
    'alien earth' must match 'Alien: Earth' even though the colon prevents a
    single-substring match."""
    from tvbf.catalog import models as m

    session.add(m.Show(id=99001, name="Alien: Earth"))
    session.add(m.Show(id=99002, name="Alien Nation"))
    session.add(m.Show(id=99003, name="Earthbound"))
    await session.commit()

    rows, _ = await list_shows(
        session, ShowFilters(search="alien earth"), sort="name", page=1, per_page=100
    )
    names = {r.name for r in rows}
    assert "Alien: Earth" in names
    assert "Alien Nation" not in names  # has 'alien' but not 'earth'
    assert "Earthbound" not in names  # has 'earth' but not 'alien'


async def test_list_shows_search_collapses_extra_whitespace(session):
    """Multiple spaces and surrounding whitespace are absorbed by split()."""
    from tvbf.catalog import models as m

    session.add(m.Show(id=99010, name="The Office"))
    await session.commit()

    rows, _ = await list_shows(
        session,
        ShowFilters(search="  the   office  "),
        sort="name",
        page=1,
        per_page=100,
    )
    assert {r.name for r in rows} == {"The Office"}


async def test_list_shows_status_filter(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(status="Ended"), sort="name", page=1, per_page=100
    )
    assert {r.name for r in rows} == {"Ancient Show", "Ended Drama"}


async def test_list_shows_language_filter(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(language="es"), sort="name", page=1, per_page=100
    )
    assert {r.name for r in rows} == {"Spanish Drama"}


async def test_list_shows_type_filter(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(type="Reality"), sort="name", page=1, per_page=100
    )
    assert {r.name for r in rows} == {"Running Reality"}


# Task 13: genre filter (AND semantics)
async def test_list_shows_single_genre(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(genres=["Crime"]), sort="name", page=1, per_page=100
    )
    assert {r.name for r in rows} == {"Multi Genre", "Running Drama"}


async def test_list_shows_multi_genre_and_semantics(session):
    await seed(session)
    rows, _ = await list_shows(
        session,
        ShowFilters(genres=["Drama", "Crime"]),
        sort="name",
        page=1,
        per_page=100,
    )
    assert {r.name for r in rows} == {"Multi Genre", "Running Drama"}


async def test_list_shows_three_genre_and_semantics(session):
    await seed(session)
    rows, _ = await list_shows(
        session,
        ShowFilters(genres=["Drama", "Crime", "Mystery"]),
        sort="name",
        page=1,
        per_page=100,
    )
    assert {r.name for r in rows} == {"Multi Genre"}


# Task 14: network filter (OR semantics)
async def test_list_shows_single_network(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(network_ids=[1]), sort="name", page=1, per_page=100
    )
    names = {r.name for r in rows}
    assert names == {"Running Drama", "Ended Drama", "Running Reality", "New Show", "TBD Show"}


async def test_list_shows_multi_network_or_semantics(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(network_ids=[1, 2]), sort="name", page=1, per_page=100
    )
    names = {r.name for r in rows}
    assert "Web Only" not in names
    assert "Ancient Show" not in names
    assert "Running Drama" in names
    assert "Spanish Drama" in names


# Task 15: sort behavior
async def test_list_shows_sort_name_desc(session):
    await seed(session)
    rows, _ = await list_shows(session, ShowFilters(), sort="-name", page=1, per_page=100)
    assert [r.name for r in rows[:2]] == ["Web Only", "TBD Show"]


async def test_list_shows_sort_premiered_desc(session):
    await seed(session)
    rows, _ = await list_shows(session, ShowFilters(), sort="-premiered", page=1, per_page=100)
    assert rows[0].name == "TBD Show"


async def test_list_shows_sort_tvmaze_updated_asc(session):
    await seed(session)
    rows, _ = await list_shows(session, ShowFilters(), sort="tvmaze_updated", page=1, per_page=100)
    assert [r.id for r in rows] == list(range(1, 11))


async def test_list_shows_sort_last_aired_desc(session):
    """`-last_aired` sorts by the most recent already-aired episode airdate.
    Shows with no aired episodes sort last (NULLS LAST)."""
    from datetime import date

    from tvbf.catalog import models as m

    session.add(m.Show(id=88001, name="Old Show"))
    session.add(m.Show(id=88002, name="Recent Show"))
    session.add(m.Show(id=88003, name="No Episodes Show"))
    await session.flush()
    session.add(
        m.Episode(
            id=88001001, show_id=88001, season_number=1, episode_number=1, air_date=date(2020, 1, 1)
        )
    )
    session.add(
        m.Episode(
            id=88002001, show_id=88002, season_number=1, episode_number=1, air_date=date(2024, 6, 1)
        )
    )
    # Future episode shouldn't count as "last aired".
    session.add(
        m.Episode(
            id=88002002, show_id=88002, season_number=1, episode_number=2, air_date=date(2099, 1, 1)
        )
    )
    await session.commit()

    rows, _ = await list_shows(
        session,
        ShowFilters(search="Show"),
        sort="-last_aired",
        page=1,
        per_page=100,
    )
    ids = [r.id for r in rows if r.id in {88001, 88002, 88003}]
    assert ids == [88002, 88001, 88003]


async def test_list_shows_invalid_sort_raises(session):
    await seed(session)
    try:
        await list_shows(session, ShowFilters(), sort="popularity", page=1, per_page=100)
    except ValueError as e:
        assert "popularity" in str(e)
    else:
        raise AssertionError("expected ValueError")


async def test_hydrate_show_refs_returns_empty_maps_for_an_empty_page(session):
    """The `if not shows` guard: an empty page must not issue queries at all."""
    assert await hydrate_show_refs(session, []) == ({}, {})


# ---------------------------------------------------------------------------
# Tombstones (ADR-0005). These moved here from the `tvmaze` tombstone module at
# the repoint: what they pin is a *read-path* rule, and the read path is
# `catalog` now. The writer that sets `deleted_upstream_at` keeps its own tests
# on both spines.
# ---------------------------------------------------------------------------


async def _add_show(session, show_id: int, *, deleted: bool = False) -> None:
    from datetime import UTC, datetime

    session.add(
        m.Show(
            id=show_id,
            tmdb_id=show_id,
            name=f"Show {show_id}",
            deleted_upstream_at=datetime.now(UTC) if deleted else None,
        )
    )
    await session.flush()


async def test_tombstoned_shows_are_hidden_from_browse_but_reachable_by_id(session):
    """Decision 3: hidden from discovery, not from someone who already has it."""
    await _add_show(session, 770)
    await _add_show(session, 771, deleted=True)
    await session.commit()

    rows, total = await list_shows(
        session, ShowFilters(search="Show 77"), sort="name", page=1, per_page=50
    )
    ids = {r.id for r in rows}
    assert 770 in ids
    assert 771 not in ids, "a tombstoned show must not be discoverable"
    assert total == len(rows)

    # Still fully serveable by id, so an existing tracker's links keep working.
    detail = await get_show_with_seasons(session, 771)
    assert detail is not None


async def test_tombstoned_show_detail_route_still_returns_200(session, authed_client):
    """At the HTTP layer, not just the query layer.

    The query-layer assertion above would keep passing if a filter were added
    in the router, which is exactly where someone would add one.
    """
    await _add_show(session, 780, deleted=True)
    await session.commit()

    resp = await authed_client.get("/shows/780")
    assert resp.status_code == 200
    assert resp.json()["id"] == 780
