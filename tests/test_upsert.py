from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.schemas import TVMazeEpisode, TVMazeNetwork, TVMazeSeason, TVMazeShow
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
            "externals": {"imdb": "tt1475582", "tvdb": 176941, "tvrage": 19718},
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
