"""Seeded catalog for browse-API tests.

Produces 10 shows spanning the filter dimensions exercised by the tests:
- Returning vs Ended vs Planned statuses
- English vs Spanish original language
- Scripted vs Reality type
- Single-genre vs multi-genre
- One network, another network, a streamer, none
- Premiered in 1990, 2010, and 2024

**Seeded into `catalog`, in TMDB's vocabulary** since NEU-1047 repointed every
read there. Two of the dimensions changed shape with the source and the fixture
carries the new one rather than the old, because a fixture that still said
`language="English"` would let the browse filter pass against data production
will never hold again:

* `status` is TMDB's string — `Returning Series` / `Ended` / `Planned` — not TV
  Maze's `Running` / `To Be Determined`. The show *names* still read "Running
  Drama" and so on; those are titles, not statuses.
* `original_language` is an ISO 639-1 code (`en`, `es`), not a language name.
* There is no web channel. TMDB draws no broadcaster/streamer distinction, so
  "Web Only" simply carries the third network.

`tmdb_synced_at` is stamped per show because that is what `tvmaze_updated` now
reads (and what `?sort=tvmaze_updated` orders by) — the old fixture set the
column of that name directly, and the epochs are carried across unchanged so the
ordering the sort tests assert is the ordering they always asserted.
"""

from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.catalog import models as m

NETWORK_A_ID = 1
NETWORK_B_ID = 2
STREAMER_ID = 100

GENRES = ["Drama", "Crime", "Comedy", "Reality", "Mystery"]


async def seed(session: AsyncSession) -> None:
    """Populate the test DB with a fixed catalog."""

    session.add(m.Network(id=NETWORK_A_ID, tmdb_id=1, name="Network A", origin_country="US"))
    session.add(m.Network(id=NETWORK_B_ID, tmdb_id=2, name="Network B", origin_country="GB"))
    session.add(m.Network(id=STREAMER_ID, tmdb_id=100, name="Web Channel X", origin_country="US"))
    await session.flush()

    genre_id_by_name: dict[str, int] = {}
    for tmdb_id, name in enumerate(GENRES, start=1):
        g = m.Genre(tmdb_id=tmdb_id, name=name)
        session.add(g)
        await session.flush()
        genre_id_by_name[name] = g.id

    # fmt: (id, name, type, status, language, premiered, genres, network_id)
    shows = [
        (
            1,
            "Running Drama",
            "Scripted",
            "Returning Series",
            "en",
            date(2020, 1, 1),
            ["Drama", "Crime"],
            NETWORK_A_ID,
        ),
        (
            2,
            "Ended Drama",
            "Scripted",
            "Ended",
            "en",
            date(2012, 1, 1),
            ["Drama"],
            NETWORK_A_ID,
        ),
        (
            3,
            "Running Comedy",
            "Scripted",
            "Returning Series",
            "en",
            date(2019, 1, 1),
            ["Comedy"],
            NETWORK_B_ID,
        ),
        (
            4,
            "Spanish Drama",
            "Scripted",
            "Returning Series",
            "es",
            date(2021, 1, 1),
            ["Drama"],
            NETWORK_B_ID,
        ),
        (
            5,
            "Running Reality",
            "Reality",
            "Returning Series",
            "en",
            date(2018, 1, 1),
            ["Reality"],
            NETWORK_A_ID,
        ),
        (
            6,
            "Ancient Show",
            "Scripted",
            "Ended",
            "en",
            date(1990, 1, 1),
            ["Drama"],
            None,
        ),
        (
            7,
            "New Show",
            "Scripted",
            "Returning Series",
            "en",
            date(2024, 6, 1),
            ["Comedy", "Drama"],
            NETWORK_A_ID,
        ),
        (
            8,
            "Web Only",
            "Scripted",
            "Returning Series",
            "en",
            date(2022, 1, 1),
            ["Drama"],
            STREAMER_ID,
        ),
        (
            9,
            "Multi Genre",
            "Scripted",
            "Returning Series",
            "en",
            date(2023, 1, 1),
            ["Drama", "Crime", "Mystery"],
            NETWORK_B_ID,
        ),
        (
            10,
            "TBD Show",
            "Scripted",
            "Planned",
            "en",
            date(2025, 1, 1),
            ["Drama"],
            NETWORK_A_ID,
        ),
    ]

    for show_id, name, type_, status_, lang, premiered, genre_names, net in shows:
        session.add(
            m.Show(
                id=show_id,
                tmdb_id=show_id,
                name=name,
                type=type_,
                status=status_,
                original_language=lang,
                first_air_date=premiered,
                tmdb_synced_at=datetime.fromtimestamp(1_700_000_000 + show_id, tz=UTC),
            )
        )
        await session.flush()
        for genre_name in genre_names:
            session.add(m.ShowGenre(show_id=show_id, genre_id=genre_id_by_name[genre_name]))
        if net is not None:
            session.add(m.ShowNetwork(show_id=show_id, network_id=net))
        for season_num in (1, 2):
            season_id = show_id * 100 + season_num
            session.add(
                m.Season(
                    id=season_id,
                    tmdb_id=season_id,
                    show_id=show_id,
                    season_number=season_num,
                    episode_count=2,
                )
            )
            await session.flush()
            for ep_num in (1, 2):
                ep_id = show_id * 1000 + season_num * 10 + ep_num
                session.add(
                    m.Episode(
                        id=ep_id,
                        tmdb_id=ep_id,
                        show_id=show_id,
                        season_id=season_id,
                        season_number=season_num,
                        episode_number=ep_num,
                        name=f"{name} S{season_num}E{ep_num}",
                    )
                )

    await session.commit()
