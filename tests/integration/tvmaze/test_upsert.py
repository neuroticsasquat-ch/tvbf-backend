from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze import upsert as upsert_module
from tvbf.tvmaze.api_payloads import TVMazeEpisode, TVMazeNetwork, TVMazeSeason, TVMazeShow
from tvbf.tvmaze.upsert import (
    upsert_episodes,
    upsert_genre_by_name,
    upsert_network,
    upsert_season,
    upsert_show,
    upsert_show_payload,
    upsert_web_channel,
)

# ---------------------------------------------------------------------------
# Task 13 — network / web_channel / genre
# ---------------------------------------------------------------------------


async def test_upsert_network_inserts_and_updates(session):
    net = TVMazeNetwork.model_validate(
        {
            "id": 1,
            "name": "CBS",
            "country": {"code": "US", "name": "USA", "timezone": "America/New_York"},
        }
    )
    net_id = await upsert_network(session, net)
    await session.commit()

    row = (await session.execute(select(m.Network).where(m.Network.id == net_id))).scalar_one()
    assert row.name == "CBS"
    assert row.country_code == "US"

    net2 = TVMazeNetwork.model_validate(
        {"id": 1, "name": "CBS (renamed)", "country": {"code": "US"}}
    )
    await upsert_network(session, net2)
    await session.commit()
    row = (
        await session.execute(
            select(m.Network).where(m.Network.id == 1),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.name == "CBS (renamed)"


async def test_upsert_web_channel_inserts(session):
    wc = TVMazeNetwork.model_validate({"id": 91, "name": "Netflix", "country": None})
    wc_id = await upsert_web_channel(session, wc)
    await session.commit()
    row = (await session.execute(select(m.WebChannel).where(m.WebChannel.id == wc_id))).scalar_one()
    assert row.name == "Netflix"
    assert row.country_code is None


async def test_upsert_network_accepts_none_returns_none(session):
    assert await upsert_network(session, None) is None
    assert await upsert_web_channel(session, None) is None


async def test_upsert_genre_by_name_is_idempotent(session):
    a = await upsert_genre_by_name(session, "Drama")
    b = await upsert_genre_by_name(session, "Drama")
    await session.commit()
    assert a == b

    c = await upsert_genre_by_name(session, "Comedy")
    assert c != a

    rows = (await session.execute(select(m.Genre))).scalars().all()
    assert {r.name for r in rows} == {"Drama", "Comedy"}


# ---------------------------------------------------------------------------
# Task 14 — seasons
# ---------------------------------------------------------------------------


async def test_upsert_season_inserts_with_fks(session):
    session.add(m.Show(id=100, name="S", tvmaze_updated=1))
    await session.commit()

    net = TVMazeNetwork.model_validate({"id": 5, "name": "BBC", "country": {"code": "GB"}})
    await upsert_network(session, net)
    season = TVMazeSeason.model_validate(
        {
            "id": 555,
            "number": 1,
            "name": "Season 1",
            "episodeOrder": 10,
            "premiereDate": "2020-01-01",
            "endDate": "2020-03-01",
            "network": {"id": 5, "name": "BBC", "country": {"code": "GB"}},
            "webChannel": None,
            "image": {"medium": "m.jpg", "original": "o.jpg"},
            "summary": "<p>summary</p>",
        }
    )
    await session.commit()

    sid = await upsert_season(session, show_id=100, season=season)
    await session.commit()
    assert sid == 555

    row = (await session.execute(select(m.Season).where(m.Season.id == 555))).scalar_one()
    assert row.show_id == 100
    assert row.number == 1
    assert row.name == "Season 1"
    assert row.episode_order == 10
    assert row.network_id == 5
    assert row.web_channel_id is None
    assert row.image_medium == "m.jpg"


async def test_upsert_season_is_idempotent(session):
    session.add(m.Show(id=101, name="S", tvmaze_updated=1))
    await session.commit()
    season = TVMazeSeason.model_validate({"id": 556, "number": 1})
    await upsert_season(session, 101, season)
    await upsert_season(session, 101, season)
    await session.commit()
    count = (await session.execute(select(m.Season).where(m.Season.id == 556))).scalars().all()
    assert len(count) == 1


# ---------------------------------------------------------------------------
# Task 15 — show + genres
# ---------------------------------------------------------------------------


async def test_upsert_show_inserts_with_genres_and_network(session):
    payload = TVMazeShow.model_validate(
        {
            "id": 200,
            "name": "Sherlock",
            "type": "Scripted",
            "language": "English",
            "status": "Ended",
            "runtime": 90,
            "premiered": "2010-07-25",
            "ended": "2017-01-15",
            "officialSite": "https://example.com",
            "summary": "<p>ok</p>",
            "image": {"medium": "m", "original": "o"},
            "externals": {"imdb": "tt1475582", "thetvdb": 176941, "tvrage": 19718},
            "network": {"id": 12, "name": "BBC One", "country": {"code": "GB"}},
            "webChannel": None,
            "genres": ["Drama", "Crime", "Mystery"],
            "updated": 1700000000,
            "_embedded": {"episodes": [], "seasons": []},
        }
    )
    await upsert_show(session, payload)
    await session.commit()

    row = (await session.execute(select(m.Show).where(m.Show.id == 200))).scalar_one()
    assert row.name == "Sherlock"
    assert row.network_id == 12
    assert row.web_channel_id is None
    assert row.externals_imdb == "tt1475582"
    assert row.externals_tvdb == 176941
    assert row.externals_tvrage == 19718
    assert row.tvmaze_updated == 1700000000

    links = (
        (
            await session.execute(
                select(m.Genre.name)
                .join(m.ShowGenre, m.ShowGenre.genre_id == m.Genre.id)
                .where(m.ShowGenre.show_id == 200)
            )
        )
        .scalars()
        .all()
    )
    assert set(links) == {"Drama", "Crime", "Mystery"}


async def test_upsert_show_replaces_genre_links_on_update(session):
    base = {
        "id": 201,
        "name": "X",
        "updated": 1,
        "network": None,
        "webChannel": None,
        "genres": ["Drama", "Crime"],
        "_embedded": {"episodes": [], "seasons": []},
    }
    await upsert_show(session, TVMazeShow.model_validate(base))
    await session.commit()

    base2 = dict(base, genres=["Comedy"])
    await upsert_show(session, TVMazeShow.model_validate(base2))
    await session.commit()

    links = (
        (
            await session.execute(
                select(m.Genre.name)
                .join(m.ShowGenre, m.ShowGenre.genre_id == m.Genre.id)
                .where(m.ShowGenre.show_id == 201)
            )
        )
        .scalars()
        .all()
    )
    assert set(links) == {"Comedy"}


# ---------------------------------------------------------------------------
# Task 16 — episodes with season_id resolution
# ---------------------------------------------------------------------------


async def test_upsert_episodes_resolves_season_id(session):
    session.add(m.Show(id=300, name="S", tvmaze_updated=1))
    await session.flush()
    session.add(m.Season(id=3000, show_id=300, number=1))
    session.add(m.Season(id=3001, show_id=300, number=2))
    await session.commit()

    eps = [
        TVMazeEpisode.model_validate(
            {"id": 1, "season": 1, "number": 1, "name": "Pilot", "airdate": "2020-01-01"}
        ),
        TVMazeEpisode.model_validate({"id": 2, "season": 1, "number": 2, "name": "Two"}),
        TVMazeEpisode.model_validate({"id": 3, "season": 2, "number": 1, "name": "S2E1"}),
        TVMazeEpisode.model_validate({"id": 4, "season": 99, "number": 1, "name": "Orphan"}),
    ]
    await upsert_episodes(session, show_id=300, episodes=eps)
    await session.commit()

    rows = (
        (
            await session.execute(
                select(m.Episode).where(m.Episode.show_id == 300).order_by(m.Episode.id)
            )
        )
        .scalars()
        .all()
    )
    by_id = {r.id: r for r in rows}
    assert by_id[1].season_id == 3000
    assert by_id[2].season_id == 3000
    assert by_id[3].season_id == 3001
    assert by_id[4].season_id is None


async def test_upsert_episodes_batches_large_shows(session):
    """Regression: shows with >2730 episodes (12 params/row × 2730 = 32760) used to
    blow the Postgres 32767 bind-parameter cap. Batching keeps each statement safe."""
    session.add(m.Show(id=500, name="S", tvmaze_updated=1))
    await session.flush()
    session.add(m.Season(id=5000, show_id=500, number=1))
    await session.commit()

    # 3000 episodes × 12 params = 36000 params — would exceed the limit unbatched.
    eps = [
        TVMazeEpisode.model_validate({"id": 100000 + i, "season": 1, "number": i, "name": f"E{i}"})
        for i in range(3000)
    ]
    await upsert_episodes(session, show_id=500, episodes=eps)
    await session.commit()

    result = await session.execute(select(m.Episode).where(m.Episode.show_id == 500))
    rows = result.scalars().all()
    assert len(rows) == 3000
    assert all(e.season_id == 5000 for e in rows)


async def test_upsert_episodes_is_idempotent_and_updates(session):
    session.add(m.Show(id=301, name="S", tvmaze_updated=1))
    await session.flush()
    session.add(m.Season(id=4000, show_id=301, number=1))
    await session.commit()

    ep_v1 = TVMazeEpisode.model_validate({"id": 10, "season": 1, "number": 1, "name": "v1"})
    ep_v2 = TVMazeEpisode.model_validate({"id": 10, "season": 1, "number": 1, "name": "v2"})
    await upsert_episodes(session, 301, [ep_v1])
    await session.commit()
    await upsert_episodes(session, 301, [ep_v2])
    await session.commit()

    row = (await session.execute(select(m.Episode).where(m.Episode.id == 10))).scalar_one()
    assert row.name == "v2"
    assert row.season_id == 4000


# ---------------------------------------------------------------------------
# Task 17 — per-show orchestration (upsert_show_payload)
# ---------------------------------------------------------------------------


async def test_upsert_show_payload_inserts_everything(session):
    payload = TVMazeShow.model_validate(
        {
            "id": 400,
            "name": "Atlanta",
            "type": "Scripted",
            "status": "Ended",
            "genres": ["Drama", "Comedy"],
            "updated": 1700000000,
            "network": {"id": 21, "name": "FX", "country": {"code": "US"}},
            "webChannel": None,
            "_embedded": {
                "seasons": [
                    {"id": 10000, "number": 1, "name": "S1", "episodeOrder": 2},
                    {"id": 10001, "number": 2, "name": "S2", "episodeOrder": 2},
                ],
                "episodes": [
                    {"id": 20000, "season": 1, "number": 1, "name": "E1"},
                    {"id": 20001, "season": 1, "number": 2, "name": "E2"},
                    {"id": 20002, "season": 2, "number": 1, "name": "E3"},
                    {"id": 20003, "season": 2, "number": 2, "name": "E4"},
                ],
            },
        }
    )
    await upsert_show_payload(session, payload)
    await session.commit()

    show = (await session.execute(select(m.Show).where(m.Show.id == 400))).scalar_one()
    assert show.network_id == 21

    seasons = (
        (await session.execute(select(m.Season).where(m.Season.show_id == 400))).scalars().all()
    )
    assert {s.number for s in seasons} == {1, 2}

    eps = (await session.execute(select(m.Episode).where(m.Episode.show_id == 400))).scalars().all()
    assert len(eps) == 4
    assert all(e.season_id is not None for e in eps)


async def test_upsert_show_payload_merges_supplied_episodes_including_specials(session):
    """The `episodes` argument carries the /shows/{id}/episodes?specials=1 result.

    It is a superset of the embed's list, so the merge keys on episode id: the
    show ends up with the embed's numbered episodes plus the special the embed
    silently dropped, and the special's null `number` round-trips.
    """
    payload = TVMazeShow.model_validate(
        {
            "id": 401,
            "name": "Chuck",
            "type": "Scripted",
            "genres": [],
            "updated": 1700000000,
            "network": None,
            "webChannel": None,
            "_embedded": {
                "seasons": [{"id": 10100, "number": 4, "name": "S4", "episodeOrder": 24}],
                "episodes": [{"id": 20100, "season": 4, "number": 1, "name": "E1"}],
            },
        }
    )
    episodes = [
        TVMazeEpisode.model_validate({"id": 20100, "season": 4, "number": 1, "name": "E1"}),
        TVMazeEpisode.model_validate(
            {
                "id": 153062,
                "season": 4,
                "number": None,
                "name": "Buy Hard: The Jeff and Lester Story",
                "airdate": "",
            }
        ),
    ]

    await upsert_show_payload(session, payload, episodes=episodes)
    await session.commit()

    eps = (await session.execute(select(m.Episode).where(m.Episode.show_id == 401))).scalars().all()
    assert {e.id for e in eps} == {20100, 153062}

    special = next(e for e in eps if e.id == 153062)
    assert special.number is None
    assert special.airdate is None
    # Specials still resolve their season FK — they belong to a real season.
    assert special.season_id == 10100


async def test_upsert_show_payload_falls_back_to_the_embed_when_episodes_is_none(session):
    """A failed specials fetch passes None, which must not wipe the embed's episodes."""
    payload = TVMazeShow.model_validate(
        {
            "id": 402,
            "name": "Fallback",
            "type": "Scripted",
            "genres": [],
            "updated": 1700000000,
            "network": None,
            "webChannel": None,
            "_embedded": {
                "seasons": [{"id": 10200, "number": 1, "name": "S1", "episodeOrder": 1}],
                "episodes": [{"id": 20200, "season": 1, "number": 1, "name": "E1"}],
            },
        }
    )

    await upsert_show_payload(session, payload, episodes=None)
    await session.commit()

    eps = (await session.execute(select(m.Episode).where(m.Episode.show_id == 402))).scalars().all()
    assert {e.id for e in eps} == {20200}


# ---------------------------------------------------------------------------
# NEU-967 — the show fetch is authoritative for a show's season set (ADR-0004)
# ---------------------------------------------------------------------------


def _show_payload(show_id: int, *, seasons: list[dict], episodes: list[dict]) -> TVMazeShow:
    return TVMazeShow.model_validate(
        {
            "id": show_id,
            "name": f"Show {show_id}",
            "type": "Scripted",
            "genres": [],
            "updated": 1700000000,
            "network": None,
            "webChannel": None,
            "_embedded": {"seasons": seasons, "episodes": episodes},
        }
    )


async def _seed_show(session, show_id: int) -> None:
    """Insert a bare show row.

    models.py declares no relationship(), so SQLAlchemy can't infer FK-based
    insert order — callers flush between this and any dependent row.
    """
    session.add(m.Show(id=show_id, name=f"Show {show_id}", tvmaze_updated=1700000000))
    await session.flush()


async def test_prune_deletes_seasons_absent_from_the_payload(session):
    """The core of NEU-967: a season upstream has deleted stops being mirrored."""
    await _seed_show(session, 410)
    session.add_all(
        [
            m.Season(id=10500, show_id=410, number=1),
            m.Season(id=10501, show_id=410, number=2),  # the phantom
        ]
    )
    await session.commit()

    payload = _show_payload(410, seasons=[{"id": 10500, "number": 1}], episodes=[])
    await upsert_show_payload(session, payload, prune_seasons=True)
    await session.commit()

    surviving = (
        (await session.execute(select(m.Season.id).where(m.Season.show_id == 410))).scalars().all()
    )
    assert set(surviving) == {10500}


async def test_prune_is_opt_in_and_defaults_to_deleting_nothing(session):
    """Trap 1: a caller that didn't request embed[]=seasons must delete nothing.

    `TVMazeEmbedded.seasons` defaults to [], so an unguarded prune would read a
    seasons-less payload as an authoritative zero and wipe the show. The default
    must therefore be inert even when the payload names no seasons at all.
    """
    await _seed_show(session, 411)
    session.add_all(
        [
            m.Season(id=10600, show_id=411, number=1),
            m.Season(id=10601, show_id=411, number=2),
        ]
    )
    await session.commit()

    payload = _show_payload(411, seasons=[], episodes=[])
    await upsert_show_payload(session, payload)  # prune_seasons defaults to False
    await session.commit()

    surviving = (
        (await session.execute(select(m.Season.id).where(m.Season.show_id == 411))).scalars().all()
    )
    assert set(surviving) == {10600, 10601}


async def test_prune_repoints_episodes_to_the_surviving_duplicate_numbered_season(session):
    """Duplicate-number repair: the phantom goes and its episodes land on the survivor.

    Show 71 upstream carried two seasons both numbered 35 and later deduplicated
    them. Because `upsert_episodes` builds its season_by_number map from a live
    query, pruning first leaves only the survivor in that map and the phantom's
    episodes are re-pointed onto it. Prune afterwards and they would instead be
    bound to a row about to vanish, and nulled by ON DELETE SET NULL.

    This asserts the OUTCOME. It does not by itself pin the ordering: with both
    rows present `season_by_number` is a dict comprehension over an unordered
    SELECT, and which duplicate wins depends on heap order, which
    `upsert_season`'s ON CONFLICT DO UPDATE perturbs via MVCC. The ordering is
    pinned deterministically by the test below instead.
    """
    await _seed_show(session, 412)
    session.add_all(
        [
            m.Season(id=10700, show_id=412, number=35),  # the survivor
            m.Season(id=10701, show_id=412, number=35),  # the phantom
        ]
    )
    await session.flush()
    session.add(m.Episode(id=20700, show_id=412, season_id=10701, season=35, number=1))
    await session.commit()

    payload = _show_payload(
        412,
        seasons=[{"id": 10700, "number": 35}],
        episodes=[{"id": 20700, "season": 35, "number": 1, "name": "E1"}],
    )
    await upsert_show_payload(session, payload, prune_seasons=True)
    await session.commit()

    surviving = (
        (await session.execute(select(m.Season.id).where(m.Season.show_id == 412))).scalars().all()
    )
    assert set(surviving) == {10700}

    ep = (
        await session.execute(
            select(m.Episode).where(m.Episode.id == 20700).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert ep.season_id == 10700, "episode should be re-pointed at the surviving season"


async def test_prune_runs_before_episodes_are_written(session, monkeypatch):
    """Pins the prune-before-episodes ordering; fails if the two steps are swapped.

    Asserts the property the ordering exists to guarantee — that by the time
    `upsert_episodes` builds its season_by_number map, the phantom is already
    gone — rather than an outcome that heap order can satisfy by accident.
    """
    await _seed_show(session, 417)
    session.add_all(
        [
            m.Season(id=11100, show_id=417, number=35),  # the survivor
            m.Season(id=11101, show_id=417, number=35),  # the phantom
        ]
    )
    await session.commit()

    seen: list[set[int]] = []
    real_upsert_episodes = upsert_module.upsert_episodes

    async def spy(session_, show_id, episodes):
        rows = (
            (await session_.execute(select(m.Season.id).where(m.Season.show_id == show_id)))
            .scalars()
            .all()
        )
        seen.append(set(rows))
        return await real_upsert_episodes(session_, show_id, episodes)

    monkeypatch.setattr(upsert_module, "upsert_episodes", spy)

    payload = _show_payload(
        417,
        seasons=[{"id": 11100, "number": 35}],
        episodes=[{"id": 21100, "season": 35, "number": 1, "name": "E1"}],
    )
    await upsert_show_payload(session, payload, prune_seasons=True)
    await session.commit()

    assert seen == [{11100}], (
        "upsert_episodes saw the phantom season — the prune must run before it, "
        f"but the visible season set was {seen}"
    )


async def test_pruned_season_without_a_survivor_leaves_episodes_null_but_reachable(session):
    """No same-numbered survivor: season_id goes NULL and the episode stays browsable.

    Covers both routes to NULL — the episode the payload still names (rewritten
    with a season_id the number map can no longer resolve) and the one it does
    not (nulled by the FK's ON DELETE SET NULL).
    """
    await _seed_show(session, 413)
    session.add(m.Season(id=10800, show_id=413, number=7))
    await session.flush()
    session.add_all(
        [
            m.Episode(id=20800, show_id=413, season_id=10800, season=7, number=1),
            m.Episode(id=20801, show_id=413, season_id=10800, season=7, number=2),
        ]
    )
    await session.commit()

    # The payload drops season 7 entirely and re-states only the first episode.
    payload = _show_payload(
        413,
        seasons=[],
        episodes=[{"id": 20800, "season": 7, "number": 1, "name": "E1"}],
    )
    await upsert_show_payload(session, payload, prune_seasons=True)
    await session.commit()

    eps = (
        (
            await session.execute(
                select(m.Episode)
                .where(m.Episode.show_id == 413)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    assert {e.id for e in eps} == {20800, 20801}
    assert all(e.season_id is None for e in eps)
    # Browse filters on the integer, not the FK, so both stay reachable.
    assert all(e.season == 7 for e in eps)


async def test_empty_payload_seasons_under_prune_is_an_authoritative_zero(session):
    """Deliberate: with the caller opted in, no seasons upstream means delete them all.

    This reads as an accident unless asserted. It is the whole reason the guard
    is a caller-supplied flag rather than an implicit `if not seasons: skip` —
    that guard would conflate this legitimate case with a missing embed.
    """
    await _seed_show(session, 414)
    session.add_all(
        [
            m.Season(id=10900, show_id=414, number=1),
            m.Season(id=10901, show_id=414, number=2),
        ]
    )
    await session.commit()

    payload = _show_payload(414, seasons=[], episodes=[])
    await upsert_show_payload(session, payload, prune_seasons=True)
    await session.commit()

    surviving = (
        (await session.execute(select(m.Season.id).where(m.Season.show_id == 414))).scalars().all()
    )
    assert surviving == []


async def test_prune_is_scoped_to_the_show_being_written(session):
    """A payload for one show must never touch another show's seasons."""
    await _seed_show(session, 415)
    await _seed_show(session, 416)
    session.add_all(
        [
            m.Season(id=11000, show_id=415, number=1),
            m.Season(id=11001, show_id=416, number=1),
        ]
    )
    await session.commit()

    payload = _show_payload(415, seasons=[], episodes=[])
    await upsert_show_payload(session, payload, prune_seasons=True)
    await session.commit()

    assert (
        (await session.execute(select(m.Season.id).where(m.Season.show_id == 415))).scalars().all()
    ) == []
    assert (
        (await session.execute(select(m.Season.id).where(m.Season.show_id == 416))).scalars().all()
    ) == [11001]
