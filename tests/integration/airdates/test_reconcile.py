"""The nightly airdate reconciliation, end to end (NEU-1145 §4.4).

The pass's own decisions — the work list, what it does with a verdict, and the
fact that a correction reaches rows already stored — as opposed to the trust
rule, which is unit-tested against `judge_seasons`.

The oracle is stubbed throughout; no test here makes a live call.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from tvbf.airdates.api_payloads import TVMazeEpisode
from tvbf.airdates.reconcile import (
    SWEEP_DAYS,
    DueReasons,
    run_airdate_reconcile,
    shows_to_check,
)
from tvbf.app import models as am
from tvbf.catalog import models as m
from tvbf.catalog.runs import create_run
from tvbf.db import SessionLocal


class FakeOracle:
    """The two calls `TVMazeOracleClient` makes, over a canned catalogue.

    Keyed by our external id so the lookup half is exercised rather than
    bypassed: a show whose id is absent here is one TV Maze has never heard of,
    which is the ordinary case the client answers `None` for. Each entry is
    minted a TV Maze id, because since NEU-1148 that id is stored — a string
    stand-in would not survive the integer column.

    `get_show_episodes` answers `None` for an id TV Maze does not serve, which
    is the signal a cached link has gone stale, and `[]` for one it serves with
    no episodes.
    """

    def __init__(self, by_external_id: dict[str, list[TVMazeEpisode]]):
        self._ids = {external: index for index, external in enumerate(by_external_id, start=1)}
        self._episodes = {self._ids[external]: eps for external, eps in by_external_id.items()}
        self.lookups = 0

    async def lookup_show(self, *, imdb_id, tvdb_id):
        self.lookups += 1
        for value in (imdb_id, tvdb_id):
            if value is not None and str(value) in self._ids:
                return self._ids[str(value)]
        return None

    async def get_show_episodes(self, show_id):
        return self._episodes.get(show_id)


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


async def _touch(session, show_id: int) -> None:
    """Advance `tmdb_synced_at`, which is what the daily delta does when TMDB
    reports a change.

    Since NEU-1149 that is also what puts a *finished* show back on the next
    night's work list, so a two-run test has to say so rather than lean on every
    show being re-checked nightly. Leaning on it would also make the test
    calendar-dependent, since the show might instead be due because tonight
    happens to be its sweep turn.
    """
    await session.execute(
        update(m.Show).where(m.Show.id == show_id).values(tmdb_synced_at=func.now())
    )
    await session.commit()


async def _stamp(session, show_id: int, *, age: timedelta = timedelta()) -> None:
    """Pretend a run reconciled this show `age` ago."""
    when = datetime.now(UTC) - age
    await session.execute(
        insert(m.AirdateShowState)
        .values(show_id=show_id, last_reconciled_at=when)
        .on_conflict_do_update(
            index_elements=[m.AirdateShowState.show_id], set_={"last_reconciled_at": when}
        )
    )
    await session.commit()


def _other_bucket(show_id: int) -> int:
    """A sweep bucket that is not this show's, so a test about some other clause
    cannot pass or fail on what day it is run."""
    return (show_id + 1) % SWEEP_DAYS


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


class TestWhatIsDue:
    """Scope says a show is ours to correct; due says tonight is its night.

    Every test here seeds a show that is in scope and due for exactly one
    reason, so a clause that stops working fails its own test rather than
    hiding behind another (NEU-1149 §9).
    """

    async def test_a_never_reconciled_show_is_due(self, session, user):
        """A new show, or one somebody just tracked. Clause 1, and the reason
        the cold start needs no backfill: every row starts NULL."""
        await _seed_show(session, show_id=840, imdb_id="tt840", dates={1: ["2001-01-01"]})
        session.add(am.UserShowWatch(user_id=user.id, show_id=840))
        await session.commit()

        due = await shows_to_check(session, today_bucket=_other_bucket(840))

        assert [s.show_id for s in due] == [840]
        assert due[0].due == DueReasons(never=True, changed=False, airing=False, swept=False)

    async def test_a_show_the_delta_touched_is_due(self, session, user):
        """Clause 2. `tmdb_synced_at` advances precisely when TMDB reported a
        change and `mirror_series` re-mirrored the show, so the trigger this
        ticket needs already existed."""
        await _seed_show(session, show_id=841, imdb_id="tt841", dates={1: ["2001-01-01"]})
        session.add(am.UserShowWatch(user_id=user.id, show_id=841))
        await session.commit()
        await _stamp(session, 841)
        await _touch(session, 841)

        due = await shows_to_check(session, today_bucket=_other_bucket(841))

        assert [s.show_id for s in due] == [841]
        assert due[0].due == DueReasons(never=False, changed=True, airing=False, swept=False)

    async def test_an_airing_show_is_due_every_night(self, session):
        """Clause 3, kept even though it readmits ~1,760 shows nobody tracks:
        an airing season's evidence changes on the *oracle's* side as TV Maze
        announces episodes, and no watermark of ours can see that."""
        future = (date.today() + timedelta(days=30)).isoformat()
        await _seed_show(session, show_id=842, imdb_id="tt842", dates={1: [future]})
        await _stamp(session, 842)

        due = await shows_to_check(session, today_bucket=_other_bucket(842))

        assert [s.show_id for s in due] == [842]
        assert due[0].due == DueReasons(never=False, changed=False, airing=True, swept=False)
        # "Every night", not "some night": no bucket leaves it out, which is the
        # difference between clause 3 and the sweep.
        for bucket in range(SWEEP_DAYS):
            assert [s.show_id for s in await shows_to_check(session, today_bucket=bucket)] == [842]

    async def test_a_finished_untouched_show_is_not_due(self, session, user):
        """The whole ticket. This is the population that grows with the user
        base — tracked shows that have finished airing — and re-deriving its
        offsets nightly buys nothing, because nothing about them can change."""
        await _seed_show(session, show_id=843, imdb_id="tt843", dates={1: ["2001-01-01"]})
        session.add(am.UserShowWatch(user_id=user.id, show_id=843))
        await session.commit()
        await _stamp(session, 843)

        assert await shows_to_check(session, today_bucket=_other_bucket(843)) == []

    async def test_the_same_show_is_due_when_its_sweep_turn_comes(self, session, user):
        """Clause 4 — and what preserves NEU-1145 §4.4's self-healing property.
        Nothing on our side can see TV Maze correcting a date on a finished show
        TMDB never touches, so without the sweep an offset that should be
        retracted never would be."""
        await _seed_show(session, show_id=844, imdb_id="tt844", dates={1: ["2001-01-01"]})
        session.add(am.UserShowWatch(user_id=user.id, show_id=844))
        await session.commit()
        await _stamp(session, 844)

        due = await shows_to_check(session, today_bucket=844 % SWEEP_DAYS)

        assert [s.show_id for s in due] == [844]
        assert due[0].due == DueReasons(never=False, changed=False, airing=False, swept=True)

    async def test_the_sweep_is_amortised_so_no_night_spikes(self, session, user):
        """One bucket per night, every show swept within `SWEEP_DAYS`, and no
        night carrying more than its share. A plain `last_reconciled_at < now() -
        7 days` would instead synchronise permanently on the first run after
        deploy: six quiet nights and one that reconciles the entire scope."""
        ids = list(range(850, 850 + 3 * SWEEP_DAYS))
        for show_id in ids:
            await _seed_show(
                session, show_id=show_id, imdb_id=f"tt{show_id}", dates={1: ["2001-01-01"]}
            )
            session.add(am.UserShowWatch(user_id=user.id, show_id=show_id))
        await session.commit()
        for show_id in ids:
            await _stamp(session, show_id)

        nights = [
            [s.show_id for s in await shows_to_check(session, today_bucket=bucket)]
            for bucket in range(SWEEP_DAYS)
        ]

        assert sorted(n for night in nights for n in night) == ids
        assert {len(night) for night in nights} == {3}

    async def test_a_show_that_has_fallen_two_intervals_behind_is_due_anyway(self, session, user):
        """The staleness floor. A missed night — container down, or a run that
        aborted on the consecutive-failure threshold before reaching this
        bucket — would otherwise cost it a full extra interval."""
        await _seed_show(session, show_id=870, imdb_id="tt870", dates={1: ["2001-01-01"]})
        session.add(am.UserShowWatch(user_id=user.id, show_id=870))
        await session.commit()
        await _stamp(session, 870, age=timedelta(days=2 * SWEEP_DAYS + 1))

        due = await shows_to_check(session, today_bucket=_other_bucket(870))

        assert [s.show_id for s in due] == [870]
        assert due[0].due.swept

    async def test_the_default_bucket_is_todays_days_since_epoch(self, session, user):
        """The override exists so the sweep's tests do not depend on the
        calendar, which leaves the default — the database's own clock — the one
        thing no other test here exercises. Days since the epoch rather than a
        day of the week, so `SWEEP_DAYS` stays a real constant instead of being
        silently pinned to 7.
        """
        ids = list(range(875, 875 + SWEEP_DAYS))
        for show_id in ids:
            await _seed_show(
                session, show_id=show_id, imdb_id=f"tt{show_id}", dates={1: ["2001-01-01"]}
            )
            session.add(am.UserShowWatch(user_id=user.id, show_id=show_id))
        await session.commit()
        for show_id in ids:
            await _stamp(session, show_id)
        tonight = (date.today() - date(1970, 1, 1)).days % SWEEP_DAYS

        by_the_clock = await shows_to_check(session)

        assert [s.show_id for s in by_the_clock] == [
            s.show_id for s in await shows_to_check(session, today_bucket=tonight)
        ]
        assert [s.show_id for s in by_the_clock] == [i for i in ids if i % SWEEP_DAYS == tonight]

    async def test_due_is_anded_onto_scope_never_substituted_for_it(self, session):
        """`last_reconciled_at IS NULL` alone matches all ~229k mirrored shows,
        which is the ~32-hour run NEU-1145 §9 refuses. A show out of scope is
        never due however many clauses it satisfies."""
        await _seed_show(session, show_id=871, imdb_id="tt871", dates={1: ["2001-01-01"]})
        await _touch(session, 871)

        assert await shows_to_check(session, today_bucket=871 % SWEEP_DAYS) == []


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
        await _touch(session, 821)

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
        # TMDB fixing its own data is a change the delta re-mirrors, which is
        # what makes the show due again — clause 2 of the work list.
        await _touch(session, 822)

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
        await _touch(session, 823)

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

    async def test_the_second_run_spends_no_lookups_at_all(self, session, user):
        """AC 1, and the whole ticket: a show resolved once is not looked up
        again, so the pass settles at one TV Maze request per show. `lookups`
        counts what the oracle was actually asked — the counter on the result is
        the same number, reported so a production run can be read the same way."""
        await _seed_show(
            session, show_id=827, imdb_id="tt827", dates={1: ["2023-05-04", "2023-05-11"]}
        )
        session.add(am.UserShowWatch(user_id=user.id, show_id=827))
        await session.commit()
        oracle = FakeOracle(
            {"tt827": [_oracle_episode(1, 1, "2023-05-05"), _oracle_episode(1, 2, "2023-05-12")]}
        )

        _, first = await _run(oracle, session=session)
        await _touch(session, 827)
        _, second = await _run(oracle, session=session)

        assert (first.lookups_spent, first.links_reused) == (1, 0)
        assert (second.lookups_spent, second.links_reused) == (0, 1)
        assert oracle.lookups == 1

    async def test_a_show_with_no_counterpart_is_not_asked_about_twice(self, session, user):
        """AC 2's first half. The ~500 shows TV Maze has never heard of are most
        of the saving, and re-looking them up nightly would give it back."""
        await _seed_show(session, show_id=828, imdb_id="tt828", dates={1: ["2023-05-04"]})
        session.add(am.UserShowWatch(user_id=user.id, show_id=828))
        await session.commit()
        oracle = FakeOracle({})

        await _run(oracle, session=session)
        await _touch(session, 828)
        _, second = await _run(oracle, session=session)

        assert oracle.lookups == 1
        assert (second.shows_not_found, second.links_reused) == (1, 1)

    async def test_a_finished_turn_is_stamped_even_when_the_oracle_has_nothing(self, session, user):
        """ "No TV Maze counterpart" is a conclusion, not an omission. Leaving it
        unstamped would keep the ~500-show negative population in every night's
        work list forever — the failure NEU-1148's negative cache prevents one
        grain down, which still costs a session and two queries here."""
        await _seed_show(session, show_id=880, imdb_id="tt880", dates={1: ["2001-01-01"]})
        session.add(am.UserShowWatch(user_id=user.id, show_id=880))
        await session.commit()

        await _run(FakeOracle({}), session=session)

        stamped = (
            await session.execute(
                select(m.AirdateShowState.last_reconciled_at)
                .where(m.AirdateShowState.show_id == 880)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert stamped is not None

    async def test_a_show_that_raised_is_due_again_on_the_next_run(self, session, user):
        """AC 9, and the one place the stamp's placement is observable: it is
        written in the same transaction as the show's own work, so a show whose
        turn blew up is left at its previous value and retried tomorrow rather
        than skipped to its sweep turn."""
        await _seed_show(session, show_id=881, imdb_id="tt881", dates={1: ["2001-01-01"]})
        session.add(am.UserShowWatch(user_id=user.id, show_id=881))
        await session.commit()

        class Exploding(FakeOracle):
            async def get_show_episodes(self, show_id):
                raise RuntimeError("upstream is having a day")

        _, result = await _run(Exploding({"tt881": []}), session=session)

        assert result.shows_failed == 1
        due = await shows_to_check(session, today_bucket=_other_bucket(881))
        assert [s.show_id for s in due] == [881]

    async def test_the_next_night_is_the_airing_set_and_tonights_bucket(self, session, user):
        """What AC 1 measures, in miniature: the tracked-finished show leaves the
        list after one run and the airing one does not, so the run's cost stops
        tracking how many shows users have accumulated."""
        future = (date.today() + timedelta(days=30)).isoformat()
        await _seed_show(session, show_id=890, imdb_id="tt890", dates={1: ["2001-01-01"]})
        await _seed_show(session, show_id=891, imdb_id="tt891", dates={1: [future]})
        session.add(am.UserShowWatch(user_id=user.id, show_id=890))
        await session.commit()
        oracle = FakeOracle({"tt890": [], "tt891": []})

        _, first = await _run(oracle, session=session)
        # A bucket belonging to neither show, so 891 is on the list because it
        # is airing and not because tonight happens to be its sweep turn.
        due = await shows_to_check(session, today_bucket=(890 + 2) % SWEEP_DAYS)

        assert (first.shows_considered, first.due_never) == (2, 2)
        assert [s.show_id for s in due] == [891]
        assert due[0].due == DueReasons(never=False, changed=False, airing=True, swept=False)

        # ...and the other half of the night: the finished show comes back on
        # its own bucket, off the stamp `mark_reconciled` actually wrote rather
        # than one a test hand-placed.
        swept = await shows_to_check(session, today_bucket=890 % SWEEP_DAYS)
        assert 890 in [s.show_id for s in swept]
        assert next(s for s in swept if s.show_id == 890).due.swept

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
