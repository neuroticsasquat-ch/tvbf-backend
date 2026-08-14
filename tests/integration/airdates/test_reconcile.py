"""The nightly airdate reconciliation, end to end (NEU-1145 §4.4).

The pass's own decisions — the work list, what it does with a verdict, and the
fact that a correction reaches rows already stored — as opposed to the trust
rule, which is unit-tested against `judge_seasons`.

The oracle is stubbed throughout; no test here makes a live call.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from tvbf.airdates.api_payloads import TVMazeEpisode
from tvbf.airdates.reconcile import run_airdate_reconcile, shows_to_check
from tvbf.app import models as am
from tvbf.catalog import models as m
from tvbf.catalog.runs import create_run
from tvbf.db import SessionLocal


class FakeOracle:
    """The two calls `TVMazeOracleClient` makes, over a canned catalogue.

    Keyed by our external id so the lookup half is exercised rather than
    bypassed: a show whose id is absent here is one TV Maze has never heard of,
    which is the ordinary case the client answers `None` for.
    """

    def __init__(self, by_external_id: dict[str, list[TVMazeEpisode]]):
        self._by_external_id = by_external_id
        self.lookups = 0

    async def lookup_show(self, *, imdb_id, tvdb_id):
        self.lookups += 1
        for value in (imdb_id, tvdb_id):
            if value is not None and str(value) in self._by_external_id:
                return str(value)
        return None

    async def get_show_episodes(self, show_id):
        return self._by_external_id[show_id]


@pytest.fixture
async def user(make_user):
    return await make_user()


def _oracle_episode(season: int, number: int, airdate: str | None) -> TVMazeEpisode:
    """Built through the real parser, so a test cannot feed the pass a shape the
    client could not have produced."""
    return TVMazeEpisode.model_validate({"season": season, "number": number, "airdate": airdate})


async def _seed_show(
    session,
    *,
    show_id: int,
    imdb_id: str | None = None,
    dates: dict[int, list[str]] | None = None,
) -> None:
    dates = dates or {}
    flat = [date.fromisoformat(v) for values in dates.values() for v in values]
    session.add(
        m.Show(
            id=show_id,
            tmdb_id=show_id,
            name=f"Show {show_id}",
            imdb_id=imdb_id,
            first_air_date=min(flat) if flat else None,
            last_air_date=max(flat) if flat else None,
        )
    )
    await session.flush()
    episode_id = show_id * 1000
    for season_number, values in dates.items():
        session.add(
            m.Season(
                id=show_id * 100 + season_number,
                tmdb_id=show_id * 100 + season_number,
                show_id=show_id,
                season_number=season_number,
                air_date=date.fromisoformat(values[0]),
            )
        )
        await session.flush()
        for number, value in enumerate(values, start=1):
            episode_id += 1
            session.add(
                m.Episode(
                    id=episode_id,
                    tmdb_id=episode_id,
                    show_id=show_id,
                    season_number=season_number,
                    episode_number=number,
                    air_date=date.fromisoformat(value),
                )
            )
    await session.flush()
    await session.commit()


def _factory():
    return SessionLocal()


async def _run(oracle, *, session) -> tuple:
    run_id = await create_run(session, kind="airdate_reconcile")
    await session.commit()
    result = await run_airdate_reconcile(session_factory=_factory, client=oracle, run_id=run_id)
    return run_id, result


async def _episodes(session, show_id: int) -> list[tuple[date | None, date | None]]:
    rows = (
        await session.execute(
            select(m.Episode.air_date, m.Episode.tmdb_air_date)
            .where(m.Episode.show_id == show_id)
            .order_by(m.Episode.season_number, m.Episode.episode_number)
            .execution_options(populate_existing=True)
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


class TestTheWorkList:
    async def test_a_tracked_show_is_in_scope(self, session, user):
        await _seed_show(session, show_id=810, imdb_id="tt810", dates={1: ["2020-01-01"]})
        session.add(am.UserShowWatch(user_id=user.id, show_id=810))
        await session.commit()

        assert [s.show_id for s in await shows_to_check(session)] == [810]

    async def test_a_show_with_an_episode_still_to_air_is_in_scope(self, session):
        """So a newly tracked show is already correct when somebody adds it,
        rather than correct one night later."""
        future = (date.today() + timedelta(days=30)).isoformat()
        await _seed_show(session, show_id=811, imdb_id="tt811", dates={1: [future]})

        assert [s.show_id for s in await shows_to_check(session)] == [811]

    async def test_a_finished_untracked_show_is_out_of_scope(self, session):
        """~229k mirrored series against a work list of ~1,762. Widening it to
        the 115,731 shows carrying an external id is a one-line change here and
        deliberately not made: ~32 hours of sustained traffic against a free,
        keyless, unfunded API is poor etiquette, and being blocked would take
        the fix down with it."""
        await _seed_show(session, show_id=812, imdb_id="tt812", dates={1: ["2001-01-01"]})

        assert await shows_to_check(session) == []

    async def test_scope_reads_the_raw_date_not_the_corrected_one(self, session):
        """A show whose only future episode is future *because* we shifted it
        forward is still in scope, and one shifted out of the future stays in.
        Reading the corrected column would make the work list depend on the
        correction it exists to establish."""
        boundary = date.today().isoformat()
        await _seed_show(session, show_id=813, imdb_id="tt813", dates={1: [boundary]})
        episode = (
            await session.execute(select(m.Episode).where(m.Episode.show_id == 813))
        ).scalar_one()
        episode.air_date = date.today() - timedelta(days=1)
        episode.tmdb_air_date = date.today() + timedelta(days=1)
        await session.commit()

        assert [s.show_id for s in await shows_to_check(session)] == [813]


class TestThePass:
    async def test_a_unanimous_shift_is_recorded_and_projected(self, session, user):
        """The end the whole ticket is for: Silo's stored rows are corrected by
        the pass itself, not at some later re-fetch that for a finished season
        never comes."""
        await _seed_show(
            session, show_id=820, imdb_id="tt820", dates={1: ["2023-05-04", "2023-05-11"]}
        )
        session.add(am.UserShowWatch(user_id=user.id, show_id=820))
        await session.commit()
        oracle = FakeOracle(
            {"tt820": [_oracle_episode(1, 1, "2023-05-05"), _oracle_episode(1, 2, "2023-05-12")]}
        )

        _, result = await _run(oracle, session=session)

        assert result.offsets_written == 1
        assert await _episodes(session, 820) == [
            (date(2023, 5, 5), date(2023, 5, 4)),
            (date(2023, 5, 12), date(2023, 5, 11)),
        ]

    async def test_a_second_run_is_a_no_op(self, session, user):
        """The comparison is against the raw value, so last night's correction
        cannot become tonight's evidence that the two sides already agree — the
        offset would retract itself on every second run."""
        await _seed_show(
            session, show_id=821, imdb_id="tt821", dates={1: ["2023-05-04", "2023-05-11"]}
        )
        session.add(am.UserShowWatch(user_id=user.id, show_id=821))
        await session.commit()
        oracle = FakeOracle(
            {"tt821": [_oracle_episode(1, 1, "2023-05-05"), _oracle_episode(1, 2, "2023-05-12")]}
        )
        await _run(oracle, session=session)

        _, result = await _run(oracle, session=session)

        assert result.offsets_retracted == 0
        assert result.rows_corrected == 0
        assert await _episodes(session, 821) == [
            (date(2023, 5, 5), date(2023, 5, 4)),
            (date(2023, 5, 12), date(2023, 5, 11)),
        ]

    async def test_agreement_retracts_an_offset_that_no_longer_holds(self, session, user):
        """TMDB fixing its own data upstream. The verdict of zero is what makes
        that self-healing rather than permanent."""
        await _seed_show(
            session, show_id=822, imdb_id="tt822", dates={1: ["2023-05-04", "2023-05-11"]}
        )
        session.add(am.UserShowWatch(user_id=user.id, show_id=822))
        await session.commit()
        shifted = FakeOracle(
            {"tt822": [_oracle_episode(1, 1, "2023-05-05"), _oracle_episode(1, 2, "2023-05-12")]}
        )
        await _run(shifted, session=session)

        agreeing = FakeOracle(
            {"tt822": [_oracle_episode(1, 1, "2023-05-04"), _oracle_episode(1, 2, "2023-05-11")]}
        )
        _, result = await _run(agreeing, session=session)

        assert result.offsets_retracted == 1
        assert await _episodes(session, 822) == [
            (date(2023, 5, 4), None),
            (date(2023, 5, 11), None),
        ]

    async def test_a_refused_season_leaves_an_established_offset_alone(self, session, user):
        """Refusing is the absence of a verdict, not a verdict of zero. An
        offset established from clean evidence must survive one night of
        ambiguous evidence — otherwise a two-episode premiere landing upstream
        would silently un-correct the whole season."""
        await _seed_show(
            session, show_id=823, imdb_id="tt823", dates={1: ["2023-05-04", "2023-05-11"]}
        )
        session.add(am.UserShowWatch(user_id=user.id, show_id=823))
        await session.commit()
        await _run(
            FakeOracle(
                {
                    "tt823": [
                        _oracle_episode(1, 1, "2023-05-05"),
                        _oracle_episode(1, 2, "2023-05-12"),
                    ]
                }
            ),
            session=session,
        )

        _, result = await _run(
            FakeOracle(
                {
                    "tt823": [
                        _oracle_episode(1, 1, "2023-05-05"),
                        _oracle_episode(1, 2, "2023-05-18"),
                    ]
                }
            ),
            session=session,
        )

        assert result.seasons_refused == 1
        assert result.offsets_retracted == 0
        assert await _episodes(session, 823) == [
            (date(2023, 5, 5), date(2023, 5, 4)),
            (date(2023, 5, 12), date(2023, 5, 11)),
        ]

    async def test_a_show_the_oracle_does_not_know_is_not_a_failure(self, session, user):
        """Most of the mirror is not on TV Maze, and counting a 404 against the
        consecutive-failure abort would stop a run that is working perfectly."""
        await _seed_show(session, show_id=824, imdb_id="tt824", dates={1: ["2023-05-04"]})
        session.add(am.UserShowWatch(user_id=user.id, show_id=824))
        await session.commit()

        run_id, result = await _run(FakeOracle({}), session=session)

        assert (result.shows_not_found, result.shows_failed) == (1, 0)
        status = (
            await session.execute(select(m.IngestRun.status).where(m.IngestRun.id == run_id))
        ).scalar_one()
        assert status == "succeeded"

    async def test_a_show_with_no_external_id_is_reported_rather_than_dropped(
        self, session, user, caplog
    ):
        """No silent caps: it is in scope and cannot be looked up, so it is
        named rather than quietly leaving the denominator."""
        await _seed_show(session, show_id=825, imdb_id=None, dates={1: ["2023-05-04"]})
        session.add(am.UserShowWatch(user_id=user.id, show_id=825))
        await session.commit()

        with caplog.at_level("WARNING"):
            _, result = await _run(FakeOracle({}), session=session)

        assert result.shows_without_external_id == 1
        assert result.shows_considered == 0
        assert "carry no imdb_id or tvdb_id" in caplog.text

    async def test_failures_are_counted_once_each(self, session, user):
        """`record_progress` *increments* `shows_failed`, so a batch tick handed
        the running total re-adds every earlier failure. With the progress batch
        at 50 and one failure early on, the column has to read 1 at the end and
        not 1-per-tick."""
        for show_id in range(830, 833):
            await _seed_show(
                session, show_id=show_id, imdb_id=f"tt{show_id}", dates={1: ["2023-05-04"]}
            )
            session.add(am.UserShowWatch(user_id=user.id, show_id=show_id))
        await session.commit()

        class Exploding(FakeOracle):
            async def get_show_episodes(self, show_id):
                raise RuntimeError("upstream is having a day")

        run_id, result = await _run(
            Exploding({f"tt{n}": [] for n in range(830, 833)}), session=session
        )

        assert result.shows_failed == 3
        run = (
            await session.execute(
                select(m.IngestRun)
                .where(m.IngestRun.id == run_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert run.shows_failed == 3

    async def test_the_run_row_is_finalized(self, session, user):
        await _seed_show(session, show_id=826, imdb_id="tt826", dates={1: ["2023-05-04"]})
        session.add(am.UserShowWatch(user_id=user.id, show_id=826))
        await session.commit()

        run_id, _ = await _run(FakeOracle({"tt826": []}), session=session)

        run = (
            await session.execute(
                select(m.IngestRun)
                .where(m.IngestRun.id == run_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert run.status == "succeeded"
        assert run.finished_at is not None
        # A date range delta hands a cursor forward; this pass has no watermark
        # at all, by design — the full work list runs every night.
        assert run.last_update_cursor is None
