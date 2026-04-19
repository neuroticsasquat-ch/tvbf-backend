"""Seeded catalog for browse-API tests.

Produces 10 shows spanning the filter dimensions exercised by the tests:
- Running vs Ended vs "To Be Determined" statuses
- English vs Spanish language
- Scripted vs Reality type
- Single-genre vs multi-genre
- Network-only, web-channel-only, both, neither
- Premiered in 1990, 2010, and 2024
"""

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m

NETWORK_A_ID = 1
NETWORK_B_ID = 2
WEB_CHANNEL_ID = 100

GENRES = ["Drama", "Crime", "Comedy", "Reality", "Mystery"]


async def seed(session: AsyncSession) -> None:
    """Populate the test DB with a fixed catalog."""

    session.add(m.Network(id=NETWORK_A_ID, name="Network A", country_code="US"))
    session.add(m.Network(id=NETWORK_B_ID, name="Network B", country_code="GB"))
    session.add(m.WebChannel(id=WEB_CHANNEL_ID, name="Web Channel X", country_code="US"))
    await session.flush()

    genre_id_by_name: dict[str, int] = {}
    for name in GENRES:
        g = m.Genre(name=name)
        session.add(g)
        await session.flush()
        genre_id_by_name[name] = g.id

    # fmt: (id, name, type, status, language, premiered, genres, network_id, web_channel_id)
    shows = [
        (
            1,
            "Running Drama",
            "Scripted",
            "Running",
            "English",
            date(2020, 1, 1),
            ["Drama", "Crime"],
            NETWORK_A_ID,
            None,
        ),
        (
            2,
            "Ended Drama",
            "Scripted",
            "Ended",
            "English",
            date(2012, 1, 1),
            ["Drama"],
            NETWORK_A_ID,
            None,
        ),
        (
            3,
            "Running Comedy",
            "Scripted",
            "Running",
            "English",
            date(2019, 1, 1),
            ["Comedy"],
            NETWORK_B_ID,
            None,
        ),
        (
            4,
            "Spanish Drama",
            "Scripted",
            "Running",
            "Spanish",
            date(2021, 1, 1),
            ["Drama"],
            NETWORK_B_ID,
            None,
        ),
        (
            5,
            "Running Reality",
            "Reality",
            "Running",
            "English",
            date(2018, 1, 1),
            ["Reality"],
            NETWORK_A_ID,
            None,
        ),
        (
            6,
            "Ancient Show",
            "Scripted",
            "Ended",
            "English",
            date(1990, 1, 1),
            ["Drama"],
            None,
            None,
        ),
        (
            7,
            "New Show",
            "Scripted",
            "Running",
            "English",
            date(2024, 6, 1),
            ["Comedy", "Drama"],
            NETWORK_A_ID,
            None,
        ),
        (
            8,
            "Web Only",
            "Scripted",
            "Running",
            "English",
            date(2022, 1, 1),
            ["Drama"],
            None,
            WEB_CHANNEL_ID,
        ),
        (
            9,
            "Multi Genre",
            "Scripted",
            "Running",
            "English",
            date(2023, 1, 1),
            ["Drama", "Crime", "Mystery"],
            NETWORK_B_ID,
            None,
        ),
        (
            10,
            "TBD Show",
            "Scripted",
            "To Be Determined",
            "English",
            date(2025, 1, 1),
            ["Drama"],
            NETWORK_A_ID,
            None,
        ),
    ]

    for show_id, name, type_, status_, lang, premiered, genre_names, net, wc in shows:
        tvmaze_updated = 1_700_000_000 + show_id
        session.add(
            m.Show(
                id=show_id,
                name=name,
                type=type_,
                status=status_,
                language=lang,
                premiered=premiered,
                network_id=net,
                web_channel_id=wc,
                tvmaze_updated=tvmaze_updated,
            )
        )
        await session.flush()
        for genre_name in genre_names:
            session.add(m.ShowGenre(show_id=show_id, genre_id=genre_id_by_name[genre_name]))
        for season_num in (1, 2):
            season_id = show_id * 100 + season_num
            session.add(m.Season(id=season_id, show_id=show_id, number=season_num, episode_order=2))
            await session.flush()
            for ep_num in (1, 2):
                ep_id = show_id * 1000 + season_num * 10 + ep_num
                session.add(
                    m.Episode(
                        id=ep_id,
                        show_id=show_id,
                        season_id=season_id,
                        season=season_num,
                        number=ep_num,
                        name=f"{name} S{season_num}E{ep_num}",
                    )
                )

    await session.commit()
