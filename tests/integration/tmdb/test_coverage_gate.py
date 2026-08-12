"""The pre-cutover go/no-go (NEU-1048).

Two halves, tested as two different kinds of thing.

The **criteria** are hard, so every test below asserts a specific way the cutover
would break user data and that the gate refuses. The pairing that matters most is
`test_accepted_exception_*`: the same unresolved row passes when it is the known,
sequenced remediation and fails when it is not, which is the difference between a
gate and a warning nobody reads.

The **coverage comparison** is a measurement, so its tests assert arithmetic —
that a TV Maze show lands in exactly one bucket, that the three outcomes partition
it, and that a dropped show with an ingested title twin is counted apart from one
without. Nothing here asserts a coverage *threshold*, because there is none:
NEU-1066 deliberately dropped 26,141 unmatched copies, so a breadth criterion
would fail by construction against a decision already taken.

Seeding is doubled wherever a user row is involved, for the reason
`test_show_prune.py` doubles it: `app.user_episode_watch` carries a foreign key
into `tvmaze.episode` while the rows being measured live in `catalog`, and the two
share an id because NEU-1042 preserved TV Maze ids as the catalog surrogates.

No upstream is mocked because none is called — every question the gate asks is
answered in Postgres.
"""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import text as sql_text

from tvbf.app.models import ActivityEvent, User, UserEpisodeWatch, UserShowWatch
from tvbf.catalog import models as cm
from tvbf.tmdb.coverage_gate import (
    ACCEPTED_UNRESOLVED,
    ADVISORY_MIN_BUCKET,
    build_gate_report,
)
from tvbf.tvmaze.models import Episode as MazeEpisode
from tvbf.tvmaze.models import Show as MazeShow

# Well clear of the browse fixtures' catalog, so every assertion can name exact ids.
_ID = 9_800_000

# Every test seeds a handful of shows, not 150,000, so the ingest floor has to
# come down or nothing below it runs. It gets its own test instead.
_NO_FLOOR = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


async def _copied_show(
    session,
    *,
    name: str = "Copied Show",
    language: str | None = "English",
    premiered: date | None = date(1995, 6, 1),
    tmdb_id: int | None = None,
    match_method: str | None = None,
    carried: bool = True,
    show_id: int | None = None,
) -> int:
    """A TV Maze show, optionally still carried as a `catalog.show` copy.

    `carried=False` is what the NEU-1066 prune leaves behind: the `tvmaze.show`
    row stands (it is the migration's denominator) and the catalog copy is gone.
    """
    show_id = show_id or _next_id()
    session.add(
        MazeShow(id=show_id, name=name, language=language, premiered=premiered, tvmaze_updated=0)
    )
    await session.flush()
    if carried:
        session.add(cm.Show(id=show_id, name=name, tmdb_id=tmdb_id, match_method=match_method))
        await session.flush()
    return show_id


async def _ingested_show(session, *, tmdb_id: int, name: str = "Ingested Show") -> int:
    """A show the TMDB ingest wrote: fresh surrogate, `tmdb_id` set, synced."""
    show_id = _next_id()
    session.add(
        cm.Show(
            id=show_id,
            name=name,
            tmdb_id=tmdb_id,
            tmdb_synced_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return show_id


async def _user(session) -> uuid.UUID:
    user = User(
        email=f"gate-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Gate",
        password_hash="x",
    )
    session.add(user)
    await session.flush()
    return user.id


async def _episode(session, *, show_id: int, mirrored: bool = True) -> int:
    """An episode in `tvmaze` and, unless `mirrored=False`, its catalog copy."""
    episode_id = _next_id()
    session.add(MazeEpisode(id=episode_id, show_id=show_id, name="Ep", season=1, number=1))
    await session.flush()
    if mirrored:
        session.add(
            cm.Episode(
                id=episode_id,
                show_id=show_id,
                name="Ep",
                season_number=1,
                episode_number=1,
            )
        )
        await session.flush()
    return episode_id


def _criterion(report, name):
    return next(c for c in report.criteria if c.name == name)


def _bucket(buckets, name):
    return next(b for b in buckets if b.bucket == name)


async def test_clean_catalog_is_a_go(session):
    """Nothing touched, nothing dangling, the ingest present — the happy path."""
    await _copied_show(session, tmdb_id=101)
    await _ingested_show(session, tmdb_id=202)
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    assert report.verdict == "go"
    assert report.failed == ()


async def test_dangling_show_fk_is_a_no_go(session):
    """The precondition NEU-1046's `ALTER TABLE` enforces, asked while it is still a report line."""
    show_id = await _copied_show(session, carried=False)
    session.add(UserShowWatch(user_id=await _user(session), show_id=show_id))
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    assert report.verdict == "no-go"
    criterion = _criterion(report, "fk_targets_resolve")
    assert not criterion.passed
    assert criterion.detail["app.user_show_watch.show_id"] == 1


async def test_dangling_episode_fk_is_a_no_go(session):
    """A watched episode with no `catalog.episode` row — the daily keeps making these."""
    show_id = await _copied_show(session)
    episode_id = await _episode(session, show_id=show_id, mirrored=False)
    session.add(UserEpisodeWatch(user_id=await _user(session), episode_id=episode_id))
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    assert report.verdict == "no-go"
    criterion = _criterion(report, "fk_targets_resolve")
    assert criterion.detail["app.user_episode_watch.episode_id"] == 1


async def test_activity_event_only_show_missing_from_catalog_is_a_no_go(session):
    """`app.activity_event` is polymorphic with no FK, so no `ALTER TABLE` would catch this.

    It neither blocks a delete nor cascades — it silently orphans, which is the
    hazard ADR-0005 cites and the reason this criterion exists separately from
    the foreign-key one.
    """
    show_id = await _copied_show(session, carried=False)
    session.add(
        ActivityEvent(
            actor_id=await _user(session),
            verb="watched_show",
            target_type="show",
            target_id=show_id,
        )
    )
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    assert report.verdict == "no-go"
    criterion = _criterion(report, "user_touched_shows_present")
    assert criterion.detail["missing_show_ids"] == [show_id]
    # And the FK criterion is silent, which is the whole point of asking twice.
    assert _criterion(report, "fk_targets_resolve").passed


async def test_unresolved_user_touched_show_is_a_no_go(session):
    """A show somebody tracks that never reached a verified mapping."""
    show_id = await _copied_show(session, name="Unmapped", tmdb_id=None)
    session.add(UserShowWatch(user_id=await _user(session), show_id=show_id))
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    assert report.verdict == "no-go"
    criterion = _criterion(report, "user_touched_shows_resolved")
    assert [row["show_id"] for row in criterion.detail["unresolved"]] == [show_id]


async def test_human_verdict_resolves_a_user_touched_show(session):
    """`match_method='human'` with a NULL id is a ruling that TMDB has no counterpart."""
    show_id = await _copied_show(session, tmdb_id=None, match_method="human")
    session.add(UserShowWatch(user_id=await _user(session), show_id=show_id))
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    assert report.verdict == "go"


async def test_accepted_exception_does_not_fail_the_gate(session):
    """The two known rows are a remediation sequenced behind NEU-1046, not a discovery."""
    show_id = next(iter(ACCEPTED_UNRESOLVED))
    await _copied_show(session, name="Discretion", tmdb_id=None, show_id=show_id)
    session.add(UserShowWatch(user_id=await _user(session), show_id=show_id))
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    assert report.verdict == "go"
    criterion = _criterion(report, "user_touched_shows_resolved")
    assert criterion.detail["unresolved"] == []
    assert [row["show_id"] for row in criterion.detail["accepted_exceptions"]] == [show_id]
    assert criterion.detail["accepted_exceptions"][0]["accepted_because"]


async def test_a_third_unresolved_row_still_fails_beside_an_accepted_one(session):
    """The exemption is a list of rows, never a licence for the shape of row."""
    user_id = await _user(session)
    accepted_id = next(iter(ACCEPTED_UNRESOLVED))
    await _copied_show(session, name="Discretion", tmdb_id=None, show_id=accepted_id)
    fresh_id = await _copied_show(session, name="Something New", tmdb_id=None)
    session.add(UserShowWatch(user_id=user_id, show_id=accepted_id))
    session.add(UserShowWatch(user_id=user_id, show_id=fresh_id))
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    assert report.verdict == "no-go"
    criterion = _criterion(report, "user_touched_shows_resolved")
    assert [row["show_id"] for row in criterion.detail["unresolved"]] == [fresh_id]


async def test_missing_ingest_is_a_no_go(session):
    """Every measurement under the floor is about a half-built catalog."""
    await _copied_show(session, tmdb_id=101)
    await session.commit()

    report = await build_gate_report(session)

    assert report.verdict == "no-go"
    criterion = _criterion(report, "ingest_present")
    assert criterion.detail["ingested_shows"] == 0
    assert criterion.detail["floor"] > 0


async def test_import_ne_absence_is_reported_not_counted_as_zero(session):
    """ "No dangling rows" and "did not look" are the two answers a gate must not conflate.

    `import_ne` is created by the Next Episode import itself, so the test database
    never has it — which is exactly the case the `to_regclass` guard exists for.
    """
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    detail = _criterion(report, "fk_targets_resolve").detail
    assert detail["import_ne_schema_present"] is False
    # None rather than 0, and rather than a sentence: the artifact is diffed, so
    # a field whose type changes between runs is the one thing a diff cannot read.
    assert detail["import_ne.show_resolution.show_id"] is None


async def test_coverage_partitions_every_tv_maze_show(session):
    """Carried, dropped-with-twin and dropped-without-twin are the three outcomes."""
    await _copied_show(session, name="Carried Matched", tmdb_id=101)
    await _copied_show(session, name="Carried Unmatched", tmdb_id=None)
    await _copied_show(session, name="Pruned With Twin", carried=False)
    await _ingested_show(session, tmdb_id=303, name="Pruned With Twin")
    await _copied_show(session, name="Pruned Alone", carried=False)
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)
    totals = report.to_dict()["coverage"]["totals"]

    assert totals["tvmaze_shows"] == 4
    assert totals["carried"] == 2
    assert totals["carried_matched"] == 1
    assert totals["dropped"] == 2
    assert totals["dropped_with_title_twin"] == 1
    assert totals["dropped_without_title_twin"] == 1
    assert totals["carried"] + totals["dropped"] == totals["tvmaze_shows"]


async def test_title_twin_is_matched_through_the_one_folded_form(session):
    """ "Shōgun" and "shogun" are the same title, decided by Postgres and nothing else."""
    await _copied_show(session, name="Shōgun", carried=False)
    await _ingested_show(session, tmdb_id=404, name="Shogun")
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    assert report.to_dict()["coverage"]["totals"]["dropped_with_title_twin"] == 1


async def test_two_ingested_rows_sharing_a_title_count_the_dropped_show_once(session):
    """The twin join is grouped per show, so a popular title cannot inflate the count."""
    await _copied_show(session, name="The Office", carried=False)
    await _ingested_show(session, tmdb_id=501, name="The Office")
    await _ingested_show(session, tmdb_id=502, name="The Office")
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)
    totals = report.to_dict()["coverage"]["totals"]

    assert totals["tvmaze_shows"] == 1
    assert totals["dropped_with_title_twin"] == 1


async def test_unmatched_ingested_row_is_not_a_title_twin(session):
    """A twin has to be a TMDB row. Another copy carrying the same title is not evidence."""
    await _copied_show(session, name="Twice Over", carried=False)
    await _copied_show(session, name="Twice Over", tmdb_id=None)
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    assert report.to_dict()["coverage"]["totals"]["dropped_without_title_twin"] == 1


async def test_language_and_era_buckets_are_named_and_complete(session):
    """The breakdown the ticket asks for: by language, and by premiere decade."""
    await _copied_show(
        session, name="Dropped RU", language="Russian", premiered=date(2003, 4, 1), carried=False
    )
    await _copied_show(
        session, name="Carried RU", language="Russian", premiered=date(2011, 4, 1), tmdb_id=101
    )
    await _copied_show(session, name="No Metadata", language=None, premiered=None, tmdb_id=102)
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    russian = _bucket(report.by_language, "Russian")
    assert (russian.tvmaze_shows, russian.carried, russian.dropped) == (2, 1, 1)
    assert russian.absent_pct == 50.0
    assert _bucket(report.by_language, "(unknown)").tvmaze_shows == 1

    assert _bucket(report.by_era, "2000s").tvmaze_shows == 1
    assert _bucket(report.by_era, "2010s").tvmaze_shows == 1
    assert _bucket(report.by_era, "(unknown)").tvmaze_shows == 1


async def test_a_small_thin_bucket_is_not_flagged_as_advisory(session):
    """A bucket of two that lost one is noise. The advisory needs a real population."""
    await _copied_show(session, name="Dropped FO", language="Faroese", carried=False)
    await _copied_show(session, name="Carried FO", language="Faroese", tmdb_id=101)
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    assert _bucket(report.by_language, "Faroese").absent_pct == 50.0
    assert report.advisory_languages == ()
    assert ADVISORY_MIN_BUCKET > 2


async def test_the_artifact_is_deterministic(session):
    """Two runs of an unchanged database are identical, which is what makes `git diff` the check."""
    await _copied_show(session, name="Dropped RU", language="Russian", carried=False)
    await _copied_show(session, name="Carried EN", language="English", tmdb_id=101)
    await session.commit()

    first = await build_gate_report(session, min_ingested=_NO_FLOOR)
    second = await build_gate_report(session, min_ingested=_NO_FLOOR)

    assert first.to_dict() == second.to_dict()
    assert [b.bucket for b in first.by_language] == sorted(b.bucket for b in first.by_language)


async def test_an_unconfirmed_tier_three_guess_is_a_no_go(session):
    """`title_year` on a user-touched show is a guess nobody has checked.

    NEU-1044's acceptance criterion is that tier-3 matches on user-touched shows
    are *"surfaced for review, not trusted silently"*, and this gate is the last
    place that can be asked. A `tmdb_id` alone is not a resolution — confirming
    one re-stamps it `'human'`, which is what moves it out of the queue and out
    of here.
    """
    show_id = await _copied_show(session, tmdb_id=101, match_method="title_year")
    session.add(UserShowWatch(user_id=await _user(session), show_id=show_id))
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    assert report.verdict == "no-go"
    criterion = _criterion(report, "user_touched_shows_resolved")
    assert [row["show_id"] for row in criterion.detail["unresolved"]] == [show_id]
    assert criterion.detail["unresolved"][0]["tmdb_id"] == 101


async def test_a_confirmed_guess_is_resolved(session):
    """The same row once a person has re-stamped it — the queue's `confirm` verdict."""
    show_id = await _copied_show(session, tmdb_id=101, match_method="human")
    session.add(UserShowWatch(user_id=await _user(session), show_id=show_id))
    await session.commit()

    assert (await build_gate_report(session, min_ingested=_NO_FLOOR)).verdict == "go"


async def test_an_exact_tier_match_is_resolved(session):
    """`/find` is an upstream assertion, not an inference — it needs no review."""
    show_id = await _copied_show(session, tmdb_id=101, match_method="tvdb_id")
    session.add(UserShowWatch(user_id=await _user(session), show_id=show_id))
    await session.commit()

    assert (await build_gate_report(session, min_ingested=_NO_FLOOR)).verdict == "go"


async def test_a_title_twin_that_disagrees_on_year_is_counted_apart(session):
    """The two twin counts bracket the truth; neither is it on its own.

    `show_prune` measured 6,464 title twins against production and only 3,337
    that also agreed on year, so a title-only count is generous by roughly half.
    """
    await _copied_show(session, name="Ghosts", premiered=date(2019, 4, 1), carried=False)
    ingested_id = await _ingested_show(session, tmdb_id=606, name="Ghosts")
    await session.execute(
        sql_text("UPDATE catalog.show SET first_air_date = :d WHERE id = :id"),
        {"d": date(1998, 1, 1), "id": ingested_id},
    )
    await session.commit()

    totals = (await build_gate_report(session, min_ingested=_NO_FLOOR)).to_dict()["coverage"][
        "totals"
    ]

    assert totals["dropped_with_title_twin"] == 1
    assert totals["dropped_with_title_and_year_twin"] == 0


async def test_a_title_twin_within_a_year_agrees(session):
    """±1 is enrichment's tier-3 tolerance, reused rather than re-decided."""
    await _copied_show(session, name="Ghosts", premiered=date(2019, 4, 1), carried=False)
    ingested_id = await _ingested_show(session, tmdb_id=606, name="Ghosts")
    await session.execute(
        sql_text("UPDATE catalog.show SET first_air_date = :d WHERE id = :id"),
        {"d": date(2020, 1, 1), "id": ingested_id},
    )
    await session.commit()

    totals = (await build_gate_report(session, min_ingested=_NO_FLOOR)).to_dict()["coverage"][
        "totals"
    ]

    assert totals["dropped_with_title_and_year_twin"] == 1


async def test_both_axes_report_advisories(session):
    """An era flag computed into the artifact and never surfaced is a number nobody can use."""
    await _copied_show(session, tmdb_id=101)
    await session.commit()

    report = await build_gate_report(session, min_ingested=_NO_FLOOR)

    assert report.advisory_languages == ()
    assert report.advisory_eras == ()
    assert report.to_dict()["coverage"]["advisory_eras"] == []
