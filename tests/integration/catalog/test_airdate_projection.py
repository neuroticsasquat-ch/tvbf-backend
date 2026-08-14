"""Projecting an offset onto rows already stored (NEU-1145).

`upsert_series_payload` corrects an airdate on the way in, which covers every
row TMDB touches from here on. It does not cover the ~6M rows already mirrored,
and for a finished season TMDB will never touch one again — so the
reconciliation pass projects, and `project_offsets` is what it projects with.

The property that makes that safe rather than a second, divergent write path is
that nothing here reads a corrected value: every write is `raw + offset`, with
`raw` taken from the `tmdb_*` twin when one is set and from the visible column
when it is not. Running it twice, running it after an ingest already applied the
same offset, and running it after the offset changed all converge.
"""

from datetime import date

from sqlalchemy import delete, select

from tvbf.catalog import models as m
from tvbf.catalog.offsets import project_offsets


async def _seed(session, *, show_id: int, seasons: dict[int, list[date]]) -> None:
    session.add(
        m.Show(
            id=show_id,
            tmdb_id=show_id,
            name=f"Show {show_id}",
            first_air_date=min(d for dates in seasons.values() for d in dates),
            last_air_date=max(d for dates in seasons.values() for d in dates),
        )
    )
    await session.flush()
    episode_id = show_id * 1000
    for number, dates in seasons.items():
        session.add(
            m.Season(
                id=show_id * 100 + number,
                tmdb_id=show_id * 100 + number,
                show_id=show_id,
                season_number=number,
                air_date=dates[0],
            )
        )
        await session.flush()
        for index, value in enumerate(dates, start=1):
            episode_id += 1
            session.add(
                m.Episode(
                    id=episode_id,
                    tmdb_id=episode_id,
                    show_id=show_id,
                    season_number=number,
                    episode_number=index,
                    air_date=value,
                )
            )
    await session.flush()


async def _offset(session, *, show_id: int, season_number: int | None, days: int) -> None:
    session.add(m.AirDateOffset(show_id=show_id, season_number=season_number, offset_days=days))
    await session.flush()


async def _episodes(session, show_id: int) -> list[tuple[int, date | None, date | None]]:
    rows = (
        await session.execute(
            select(m.Episode.episode_number, m.Episode.air_date, m.Episode.tmdb_air_date)
            .where(m.Episode.show_id == show_id)
            .order_by(m.Episode.season_number, m.Episode.episode_number)
            .execution_options(populate_existing=True)
        )
    ).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def _show_row(session, show_id: int) -> m.Show:
    return (
        await session.execute(
            select(m.Show).where(m.Show.id == show_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


class TestProjection:
    async def test_it_shifts_the_stored_rows_and_records_the_raw_value(self, session):
        await _seed(session, show_id=700, seasons={1: [date(2023, 5, 4), date(2023, 5, 11)]})
        await _offset(session, show_id=700, season_number=1, days=1)

        assert await project_offsets(session, show_id=700) > 0

        assert await _episodes(session, 700) == [
            (1, date(2023, 5, 5), date(2023, 5, 4)),
            (2, date(2023, 5, 12), date(2023, 5, 11)),
        ]

    async def test_running_it_again_changes_nothing(self, session):
        """The invariant is `corrected = raw + offset`, not a history of edits.
        A pass that added the offset to whatever it found stored would move
        every date another day every night."""
        await _seed(session, show_id=701, seasons={1: [date(2023, 5, 4), date(2023, 5, 11)]})
        await _offset(session, show_id=701, season_number=1, days=1)
        await project_offsets(session, show_id=701)

        assert await project_offsets(session, show_id=701) == 0
        assert await _episodes(session, 701) == [
            (1, date(2023, 5, 5), date(2023, 5, 4)),
            (2, date(2023, 5, 12), date(2023, 5, 11)),
        ]

    async def test_changing_an_offset_re_derives_from_the_raw_value(self, session):
        await _seed(session, show_id=702, seasons={1: [date(2023, 5, 4), date(2023, 5, 11)]})
        await _offset(session, show_id=702, season_number=1, days=1)
        await project_offsets(session, show_id=702)

        offset = (
            await session.execute(select(m.AirDateOffset).where(m.AirDateOffset.show_id == 702))
        ).scalar_one()
        offset.offset_days = -1
        await session.flush()

        await project_offsets(session, show_id=702)
        assert await _episodes(session, 702) == [
            (1, date(2023, 5, 3), date(2023, 5, 4)),
            (2, date(2023, 5, 10), date(2023, 5, 11)),
        ]

    async def test_deleting_an_offset_restores_the_upstream_value(self, session):
        """Retraction, which is what makes an operator's `DELETE` and the job's
        own verdict-of-zero mean the same thing."""
        await _seed(session, show_id=703, seasons={1: [date(2023, 5, 4)]})
        await _offset(session, show_id=703, season_number=1, days=1)
        await project_offsets(session, show_id=703)

        await session.execute(delete(m.AirDateOffset).where(m.AirDateOffset.show_id == 703))
        await project_offsets(session, show_id=703)

        assert await _episodes(session, 703) == [(1, date(2023, 5, 4), None)]

    async def test_a_season_without_an_offset_is_untouched(self, session):
        """Ted Lasso: seasons 1-2 are already right and must not move."""
        await _seed(
            session,
            show_id=704,
            seasons={1: [date(2020, 8, 14), date(2020, 8, 21)], 3: [date(2023, 3, 14)]},
        )
        await _offset(session, show_id=704, season_number=3, days=1)

        await project_offsets(session, show_id=704)

        assert await _episodes(session, 704) == [
            (1, date(2020, 8, 14), None),
            (2, date(2020, 8, 21), None),
            (1, date(2023, 3, 15), date(2023, 3, 14)),
        ]

    async def test_a_show_wide_default_reaches_every_season(self, session):
        await _seed(session, show_id=705, seasons={1: [date(2020, 8, 14)], 2: [date(2021, 7, 23)]})
        await _offset(session, show_id=705, season_number=None, days=1)

        await project_offsets(session, show_id=705)

        assert await _episodes(session, 705) == [
            (1, date(2020, 8, 15), date(2020, 8, 14)),
            (1, date(2021, 7, 24), date(2021, 7, 23)),
        ]

    async def test_the_season_row_takes_its_own_offset(self, session):
        """Or the correction manufactures a contradiction on a single page — a
        season premiere reading a day before its own first episode."""
        await _seed(session, show_id=706, seasons={1: [date(2023, 5, 4), date(2023, 5, 11)]})
        await _offset(session, show_id=706, season_number=1, days=1)

        await project_offsets(session, show_id=706)

        season = (
            await session.execute(
                select(m.Season)
                .where(m.Season.show_id == 706)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert (season.air_date, season.tmdb_air_date) == (date(2023, 5, 5), date(2023, 5, 4))


class TestShowGrain:
    async def test_the_premiere_moves_only_when_season_one_did(self, session):
        """Ted Lasso is the whole reason the two show-grain dates are keyed to
        different seasons: its premiere is right and its last-aired date is a
        day early, so a blanket per-show shift breaks one to fix the other."""
        await _seed(
            session,
            show_id=707,
            seasons={
                1: [date(2020, 8, 14), date(2020, 8, 21)],
                3: [date(2023, 3, 14), date(2023, 3, 21)],
            },
        )
        await _offset(session, show_id=707, season_number=3, days=1)

        await project_offsets(session, show_id=707)

        show = await _show_row(session, 707)
        assert (show.first_air_date, show.tmdb_first_air_date) == (date(2020, 8, 14), None)
        assert (show.last_air_date, show.tmdb_last_air_date) == (
            date(2023, 3, 22),
            date(2023, 3, 21),
        )

    async def test_the_show_dates_are_idempotent_too(self, session):
        await _seed(session, show_id=708, seasons={1: [date(2023, 5, 4), date(2023, 5, 11)]})
        await _offset(session, show_id=708, season_number=1, days=1)
        await project_offsets(session, show_id=708)
        first = await _show_row(session, 708)
        before = (first.first_air_date, first.last_air_date)

        await project_offsets(session, show_id=708)

        show = await _show_row(session, 708)
        assert (show.first_air_date, show.last_air_date) == before
        assert show.tmdb_first_air_date == date(2023, 5, 4)
