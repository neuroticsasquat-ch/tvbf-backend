"""The AC 2 / AC 3 verification harness (NEU-1145 §7).

The properties that make the harness worth trusting are all about what it
*refuses* to conclude: that a repaired row and an always-correct row are told
apart, that an over-correction is caught rather than counted as progress, and
that a row which stopped being comparable is a loss rather than a quiet
improvement to the percentage.
"""

from datetime import UTC, date, datetime
from uuid import uuid4

from tvbf.airdates.verify import (
    AGREES,
    ONE_DAY_EARLY,
    ONE_DAY_LATE,
    OTHER,
    UNRESOLVED,
    build_snapshot,
    compare,
    load_archive_rows,
    show_report,
)
from tvbf.app import models as am
from tvbf.catalog import models as m


async def _show(session, *, show_id: int, name: str, network: str | None = None) -> None:
    session.add(m.Show(id=show_id, tmdb_id=show_id, name=name))
    await session.flush()
    if network is not None:
        session.add(m.Network(id=show_id, tmdb_id=show_id, name=network))
        await session.flush()
        session.add(m.ShowNetwork(show_id=show_id, network_id=show_id))
        await session.flush()


async def _episode(
    session,
    *,
    episode_id: int,
    show_id: int,
    season: int,
    number: int,
    air_date: date,
    tmdb_air_date: date | None = None,
) -> None:
    session.add(
        m.Episode(
            id=episode_id,
            tmdb_id=episode_id,
            show_id=show_id,
            season_number=season,
            episode_number=number,
            air_date=air_date,
            tmdb_air_date=tmdb_air_date,
        )
    )
    await session.flush()


async def _archived(
    session,
    *,
    show_id: int,
    episode_id: int | None,
    show_name: str,
    season: int,
    number: int,
    airdate: date,
) -> None:
    session.add(
        am.WatchArchive(
            record_type="episode_watch",
            user_id=uuid4(),
            user_email="viewer@example.com",
            user_display_name="Viewer",
            show_name=show_name,
            season_number=season,
            episode_number=number,
            episode_airdate=airdate,
            occurred_at=datetime.now(UTC),
            source_show_id=show_id,
            source_episode_id=episode_id,
        )
    )
    await session.flush()


async def _snapshot(session) -> dict:
    return build_snapshot(await load_archive_rows(session))


class TestBucketing:
    async def test_a_date_before_the_archived_one_reads_as_a_day_early(self, session):
        """The bug's own signature. TV Maze snapshotted the Eastern date
        pre-cutover; TMDB recording the Pacific day puts ours before it."""
        await _show(session, show_id=910, name="Silo", network="Apple TV+")
        await _episode(
            session, episode_id=9101, show_id=910, season=1, number=1, air_date=date(2023, 5, 4)
        )
        await _archived(
            session,
            show_id=910,
            episode_id=9101,
            show_name="Silo",
            season=1,
            number=1,
            airdate=date(2023, 5, 5),
        )

        snapshot = await _snapshot(session)

        assert snapshot["totals"][ONE_DAY_EARLY] == 1
        assert snapshot["by_network"]["Apple TV+"][ONE_DAY_EARLY] == 1

    async def test_a_corrected_row_reads_as_agreeing(self, session):
        await _show(session, show_id=911, name="Silo", network="Apple TV+")
        await _episode(
            session,
            episode_id=9111,
            show_id=911,
            season=1,
            number=1,
            air_date=date(2023, 5, 5),
            tmdb_air_date=date(2023, 5, 4),
        )
        await _archived(
            session,
            show_id=911,
            episode_id=9111,
            show_name="Silo",
            season=1,
            number=1,
            airdate=date(2023, 5, 5),
        )

        assert (await _snapshot(session))["totals"][AGREES] == 1

    async def test_an_over_correction_is_its_own_bucket(self, session):
        """Not folded into `other`: a day *late* is the signature of an
        over-correction, which is exactly what a per-network or weekday rule
        would have produced on Prime Video, and a bare "does it agree now?"
        check cannot see it."""
        await _show(session, show_id=912, name="Overshot", network="Prime Video")
        await _episode(
            session, episode_id=9121, show_id=912, season=1, number=1, air_date=date(2023, 5, 6)
        )
        await _archived(
            session,
            show_id=912,
            episode_id=9121,
            show_name="Overshot",
            season=1,
            number=1,
            airdate=date(2023, 5, 5),
        )

        assert (await _snapshot(session))["totals"][ONE_DAY_LATE] == 1

    async def test_a_row_resolves_by_season_and_episode_not_by_the_archived_id(self, session):
        """The archive's episode ids are a pre-cutover snapshot, and NEU-1126
        and NEU-1146 moved out from under them. The show id survived both, so
        the season/episode pair is the durable key."""
        await _show(session, show_id=913, name="Repointed", network="Apple TV+")
        await _episode(
            session, episode_id=9131, show_id=913, season=2, number=3, air_date=date(2024, 1, 2)
        )
        await _archived(
            session,
            show_id=913,
            # The id the row was archived against no longer exists.
            episode_id=999_999_999,
            show_name="Repointed",
            season=2,
            number=3,
            airdate=date(2024, 1, 2),
        )

        assert (await _snapshot(session))["totals"][AGREES] == 1

    async def test_an_episode_that_no_longer_exists_is_unresolved_not_dropped(self, session):
        """Silently leaving the denominator is how a regression hides inside an
        improving percentage."""
        await _show(session, show_id=914, name="Retired", network="Apple TV+")
        await _archived(
            session,
            show_id=914,
            episode_id=888_888_888,
            show_name="Retired",
            season=1,
            number=1,
            airdate=date(2024, 1, 2),
        )

        assert (await _snapshot(session))["totals"][UNRESOLVED] == 1


class TestCompare:
    def _snap(self, rows: dict[str, tuple[str, int | None]]) -> dict:
        return {
            "totals": {},
            "rows": {
                key: {
                    "bucket": bucket,
                    "delta_days": delta,
                    "show": "Show",
                    "season": 1,
                    "episode": 1,
                    "archived_airdate": "2023-05-05",
                    "catalog_airdate": "2023-05-05",
                }
                for key, (bucket, delta) in rows.items()
            },
        }

    def test_a_repaired_row_is_a_correction(self):
        diff = compare(self._snap({"1": (ONE_DAY_EARLY, -1)}), self._snap({"1": (AGREES, 0)}))
        assert len(diff["corrected"]) == 1
        assert diff["regressed"] == []

    def test_an_always_correct_row_is_neither(self):
        """AC 3's second claim: *the already-correct rows are untouched*. It is
        the claim totals cannot make, because a hundred repairs plus a hundred
        new breakages sums the same as leaving everything alone."""
        diff = compare(self._snap({"1": (AGREES, 0)}), self._snap({"1": (AGREES, 0)}))
        assert diff["corrected"] == []
        assert diff["regressed"] == []

    def test_moving_a_correct_row_is_a_regression(self):
        diff = compare(self._snap({"1": (AGREES, 0)}), self._snap({"1": (ONE_DAY_LATE, 1)}))
        assert len(diff["regressed"]) == 1

    def test_an_over_correction_is_a_regression_not_a_movement(self):
        """A day early becoming a day late leaves `abs(delta)` identical, so a
        grew-bigger rule alone files it as progress. It is not: we took a date
        that was one day wrong and made it one day wrong the other way, on
        purpose. It is the signature of the per-network rule §2.6 rejected."""
        diff = compare(self._snap({"1": (ONE_DAY_EARLY, -1)}), self._snap({"1": (ONE_DAY_LATE, 1)}))
        assert len(diff["regressed"]) == 1
        assert diff["other_movements"] == []

    def test_a_worse_disagreement_in_the_same_direction_is_a_regression(self):
        diff = compare(self._snap({"1": (ONE_DAY_EARLY, -1)}), self._snap({"1": (OTHER, -6)}))
        assert len(diff["regressed"]) == 1

    def test_a_smaller_disagreement_in_the_same_direction_is_progress(self):
        """Shrinking S3 if it were ever corrected by hand: still wrong, but less
        wrong, and in the same direction. Progress, not a regression."""
        diff = compare(self._snap({"1": (OTHER, -6)}), self._snap({"1": (ONE_DAY_EARLY, -1)}))
        assert diff["regressed"] == []
        assert len(diff["other_movements"]) == 1

    def test_a_row_that_stops_being_comparable_is_a_regression(self):
        diff = compare(self._snap({"1": (AGREES, 0)}), self._snap({"1": (UNRESOLVED, None)}))
        assert len(diff["regressed"]) == 1

    def test_a_row_added_since_the_baseline_is_reported_not_scored(self):
        """`app.watch_archive` is append-only and grows as people watch things,
        so a new row is the app working rather than a change to be judged."""
        diff = compare(self._snap({}), self._snap({"7": (ONE_DAY_EARLY, -1)}))
        assert diff["added"] == ["7"]
        assert diff["regressed"] == []
        assert diff["corrected"] == []

    def test_rows_still_early_are_listed_but_do_not_fail(self):
        """Some cannot be corrected on purpose — a show TV Maze does not carry,
        and a season the trust rule refused (Shrinking S3)."""
        diff = compare(
            self._snap({"1": (ONE_DAY_EARLY, -1)}), self._snap({"1": (ONE_DAY_EARLY, -1)})
        )
        assert [r["archive_id"] for r in diff["still_early"]] == [1]
        assert diff["regressed"] == []


class TestShowReport:
    async def test_it_puts_the_served_date_beside_the_raw_one(self, session):
        """AC 2 cannot be automated — there is no machine-readable Apple
        schedule — so the report's job is to make the comparison a human does
        one glance wide."""
        await _show(session, show_id=920, name="Silo", network="Apple TV+")
        await _episode(
            session,
            episode_id=9201,
            show_id=920,
            season=1,
            number=1,
            air_date=date(2023, 5, 5),
            tmdb_air_date=date(2023, 5, 4),
        )
        session.add(m.AirDateOffset(show_id=920, season_number=1, offset_days=1))
        await session.flush()

        report = await show_report(session, show_names=["Silo"])

        show = report["shows"][0]["matched"][0]
        assert show["offsets"] == {"1": 1}
        assert show["recent_episodes"][0]["air_date"] == "2023-05-05"
        assert show["recent_episodes"][0]["tmdb_air_date"] == "2023-05-04"
        assert show["recent_episodes"][0]["offset_applied"] == 1

    async def test_a_show_the_catalog_does_not_carry_is_reported_as_unmatched(self, session):
        report = await show_report(session, show_names=["Not A Real Show"])
        assert report["shows"][0]["matched"] == []
