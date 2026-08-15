"""The airdate correction on the ingest write path (NEU-1145 §4.3, AC 7).

Correcting on the way in is what keeps every reader right without threading a
correction through browse, Upcoming, Watch Next, the aired filter and three page
types. The property that makes it survivable is that the corrected column and
its raw twin are only ever written **together**, from the payload value in hand:
`corrected = raw + offset` is an invariant rather than a history of edits.

The last test here is the one the whole pairing exists for. A design that
re-applied the offset to whatever it found stored cannot tell a re-run from a
genuine upstream ±1 day change, and would swallow the second while
double-applying on the first.
"""

from datetime import date

from sqlalchemy import select

from tests.fixtures.tmdb.series_factory import make_episode, make_season_detail, make_series
from tvbf.catalog import models as m
from tvbf.tmdb.api_payloads import TMDBSeries
from tvbf.tmdb.upsert import upsert_series_payload

TMDB_ID = 8801


def _payload(*, air_dates: dict[int, str], first: str, last: str) -> dict:
    """A one-season series whose episodes carry the given dates."""
    payload = make_series(
        TMDB_ID,
        seasons=1,
        episodes_per_season=len(air_dates),
        append_seasons=False,
        first_air_date=first,
        last_air_date=last,
    )
    payload["seasons"][0]["air_date"] = first
    payload["season/1"] = make_season_detail(
        1,
        [
            make_episode(TMDB_ID * 10000 + 100 + number, 1, number, air_date=value)
            for number, value in sorted(air_dates.items())
        ],
        air_date=first,
    )
    return payload


async def _write(session, payload: dict) -> int:
    show_id = await upsert_series_payload(session, TMDBSeries.model_validate(payload))
    await session.commit()
    return show_id


async def _episodes(session, show_id: int) -> list[tuple[date | None, date | None]]:
    rows = (
        await session.execute(
            select(m.Episode.air_date, m.Episode.tmdb_air_date)
            .where(m.Episode.show_id == show_id)
            .order_by(m.Episode.episode_number)
            .execution_options(populate_existing=True)
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


async def _show(session, show_id: int) -> m.Show:
    return (
        await session.execute(
            select(m.Show).where(m.Show.id == show_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _record_offset(session, *, show_id: int, season_number: int, days: int) -> None:
    session.add(m.AirDateOffset(show_id=show_id, season_number=season_number, offset_days=days))
    await session.commit()


class TestTheIngestAppliesTheOffset:
    async def test_a_show_with_no_offset_stores_tmdb_verbatim(self, session):
        """The overwhelming majority of the ~6.0M dated rows. NULL twins are
        what keep the coverage audit's claim literally true for them."""
        show_id = await _write(
            session,
            _payload(
                air_dates={1: "2023-05-04", 2: "2023-05-11"},
                first="2023-05-04",
                last="2023-05-11",
            ),
        )
        assert await _episodes(session, show_id) == [
            (date(2023, 5, 4), None),
            (date(2023, 5, 11), None),
        ]

    async def test_a_re_fetch_applies_the_offset_at_every_grain(self, session):
        """Episode, season and both show dates — or the correction manufactures
        a contradiction on a single page, with a premiere reading a day before
        the season 1 episode 1 printed underneath it."""
        payload = _payload(
            air_dates={1: "2023-05-04", 2: "2023-05-11"}, first="2023-05-04", last="2023-05-11"
        )
        show_id = await _write(session, payload)
        await _record_offset(session, show_id=show_id, season_number=1, days=1)

        await _write(session, payload)

        assert await _episodes(session, show_id) == [
            (date(2023, 5, 5), date(2023, 5, 4)),
            (date(2023, 5, 12), date(2023, 5, 11)),
        ]
        season = (
            await session.execute(
                select(m.Season)
                .where(m.Season.show_id == show_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert (season.air_date, season.tmdb_air_date) == (date(2023, 5, 5), date(2023, 5, 4))

        show = await _show(session, show_id)
        assert (show.first_air_date, show.tmdb_first_air_date) == (
            date(2023, 5, 5),
            date(2023, 5, 4),
        )
        assert (show.last_air_date, show.tmdb_last_air_date) == (
            date(2023, 5, 12),
            date(2023, 5, 11),
        )

    async def test_re_running_the_same_payload_does_not_double_apply(self, session):
        """AC 7's first half. This is the failure a re-apply-after-the-delta
        design has no defence against: it would move every date another day on
        every pass, forever, silently."""
        payload = _payload(
            air_dates={1: "2023-05-04", 2: "2023-05-11"}, first="2023-05-04", last="2023-05-11"
        )
        show_id = await _write(session, payload)
        await _record_offset(session, show_id=show_id, season_number=1, days=1)

        for _ in range(3):
            await _write(session, payload)

        assert await _episodes(session, show_id) == [
            (date(2023, 5, 5), date(2023, 5, 4)),
            (date(2023, 5, 12), date(2023, 5, 11)),
        ]

    async def test_a_genuine_upstream_one_day_change_is_picked_up(self, session):
        """AC 7's second half, and the reason the pair is written together from
        the payload rather than derived from what is stored. TMDB moving an
        episode by exactly one day looks identical to a correction already
        applied — unless the raw value is recorded, which it is."""
        show_id = await _write(
            session,
            _payload(
                air_dates={1: "2023-05-04", 2: "2023-05-11"},
                first="2023-05-04",
                last="2023-05-11",
            ),
        )
        await _record_offset(session, show_id=show_id, season_number=1, days=1)
        await _write(
            session,
            _payload(
                air_dates={1: "2023-05-04", 2: "2023-05-11"},
                first="2023-05-04",
                last="2023-05-11",
            ),
        )

        # Upstream reschedules episode 2 by one day.
        await _write(
            session,
            _payload(
                air_dates={1: "2023-05-04", 2: "2023-05-12"},
                first="2023-05-04",
                last="2023-05-12",
            ),
        )

        assert await _episodes(session, show_id) == [
            (date(2023, 5, 5), date(2023, 5, 4)),
            (date(2023, 5, 13), date(2023, 5, 12)),
        ]

    async def test_an_undated_episode_stays_undated(self, session):
        payload = _payload(
            air_dates={1: "2023-05-04", 2: "2023-05-11"}, first="2023-05-04", last="2023-05-11"
        )
        payload["season/1"]["episodes"][1]["air_date"] = ""
        show_id = await _write(session, payload)
        await _record_offset(session, show_id=show_id, season_number=1, days=1)

        await _write(session, payload)

        assert await _episodes(session, show_id) == [
            (date(2023, 5, 5), date(2023, 5, 4)),
            (None, None),
        ]
