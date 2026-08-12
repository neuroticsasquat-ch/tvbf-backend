"""Episode-grain re-point (NEU-1126).

Every test here is one of the ticket's acceptance criteria or one of the ways
this pass could cost somebody their watch history — which is a bigger surface
than `season_dedupe`'s, because this is the one migration pass that writes to
`app`.

The two that matter most are the ones asserting what is *kept*: an episode with
no TMDB counterpart is the only row that watch record can point at, and `tvmaze`
is the only place it could come back from.

Seeding is doubled wherever a watch record is involved, for the reason
`test_season_dedupe.py` doubles it: `app.user_episode_watch` references
`catalog.episode` and the two spines share an id because NEU-1042 preserved TV
Maze ids as the catalog surrogates.

No upstream is mocked because none is called — every question this pass asks is
answered in Postgres.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from tvbf.app.models import ActivityEvent, UserEpisodeRating, UserEpisodeWatch
from tvbf.catalog import models as cm
from tvbf.tmdb.episode_repoint import (
    EpisodeRepointAborted,
    IngestNotRun,
    build_report,
    repoint_episodes,
)

# Well clear of the browse fixtures' catalog, so every assertion can name exact ids.
_ID = 9_850_000

# Every test seeds a handful of shows, not 150,000, so the ingest floor has to
# come down or nothing below it runs. It gets its own test instead.
_NO_FLOOR = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


async def _show(session, *, tmdb_id: int | None, name: str = "Repoint Show") -> int:
    """A `catalog.show`. `tmdb_id=None` is the locally-authored (unmatched) case."""
    show_id = _next_id()
    session.add(
        cm.Show(
            id=show_id,
            name=name,
            tmdb_id=tmdb_id,
            tmdb_synced_at=datetime.now(UTC) if tmdb_id else None,
        )
    )
    await session.flush()
    return show_id


async def _episode(
    session,
    show_id: int,
    *,
    tmdb_id: int | None,
    season: int = 1,
    number: int = 1,
) -> int:
    """An episode. `tmdb_id=None` is a copied row, set is one the ingest wrote."""
    episode_id = _next_id()
    session.add(
        cm.Episode(
            id=episode_id,
            show_id=show_id,
            season_number=season,
            episode_number=number,
            tmdb_id=tmdb_id,
        )
    )
    await session.flush()
    return episode_id


async def _pair(session, *, season: int = 1, number: int = 1) -> tuple[int, int, int]:
    """A matched show carrying one copied episode and its ingested twin."""
    show_id = await _show(session, tmdb_id=_next_id())
    copy = await _episode(session, show_id, tmdb_id=None, season=season, number=number)
    twin = await _episode(session, show_id, tmdb_id=_next_id(), season=season, number=number)
    return show_id, copy, twin


async def _episode_ids(session, show_id: int) -> list[int]:
    rows = await session.execute(
        select(cm.Episode.id).where(cm.Episode.show_id == show_id).order_by(cm.Episode.id)
    )
    return list(rows.scalars())


async def _watched_episode(session, user_id, episode_id: int) -> None:
    session.add(UserEpisodeWatch(user_id=user_id, episode_id=episode_id))
    await session.flush()


async def _watch_targets(session, user_id) -> list[int]:
    rows = await session.execute(
        select(UserEpisodeWatch.episode_id)
        .where(UserEpisodeWatch.user_id == user_id)
        .order_by(UserEpisodeWatch.episode_id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars())


@pytest.mark.asyncio
async def test_the_watch_moves_to_the_twin_and_the_copy_goes(session, make_user):
    """The ticket in one test: 6,948 production watches should end up here."""
    user = await make_user(email="er1@example.com")
    show_id, copy, twin = await _pair(session)
    await _watched_episode(session, user.id, copy)
    await session.commit()

    result = await repoint_episodes(session, min_ingested=_NO_FLOOR)

    assert result.watches_repointed == 1
    assert result.episodes_deleted == 1
    assert await _watch_targets(session, user.id) == [twin]
    assert await _episode_ids(session, show_id) == [twin]


@pytest.mark.asyncio
async def test_ratings_and_activity_events_move_too(session, make_user):
    """Three write sites, and only two of them would fail loudly if forgotten.

    `app.activity_event` is polymorphic with no foreign key at all — it neither
    blocks nor cascades, it silently orphans — which is why it is asserted here
    rather than left to the constraint that does not exist.
    """
    user = await make_user(email="er2@example.com")
    _, copy, twin = await _pair(session)
    session.add(UserEpisodeRating(user_id=user.id, episode_id=copy, stars=Decimal("4.0")))
    session.add(
        ActivityEvent(actor_id=user.id, verb="watched", target_type="episode", target_id=copy)
    )
    await session.commit()

    result = await repoint_episodes(session, min_ingested=_NO_FLOOR)

    assert (result.ratings_repointed, result.activity_repointed) == (1, 1)
    rated = (
        await session.execute(
            select(UserEpisodeRating.episode_id)
            .where(UserEpisodeRating.user_id == user.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    event = (
        await session.execute(
            select(ActivityEvent.target_id)
            .where(ActivityEvent.actor_id == user.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert (rated, event) == (twin, twin)


@pytest.mark.asyncio
async def test_an_episode_with_no_twin_keeps_its_row_and_its_history(session, make_user):
    """The 189 production episodes TMDB has no counterpart for.

    ADR-0008 sanctions the locally-authored row; deleting one destroys history
    nothing can restore, so this is the assertion that matters most here.
    """
    user = await make_user(email="er3@example.com")
    show_id = await _show(session, tmdb_id=_next_id())
    orphan = await _episode(session, show_id, tmdb_id=None, season=9, number=1)
    await _watched_episode(session, user.id, orphan)
    await session.commit()

    result = await repoint_episodes(session, min_ingested=_NO_FLOOR)

    assert result.episodes_deleted == 0
    assert await _episode_ids(session, show_id) == [orphan]
    assert await _watch_targets(session, user.id) == [orphan]


@pytest.mark.asyncio
async def test_a_copied_special_is_never_paired(session, make_user):
    """NEU-1042 numbers a null-numbered TV Maze special negative within its season.

    No ingested row carries a negative `episode_number`, so the special finds no
    twin and is kept — the right outcome, reached without a special case.
    """
    user = await make_user(email="er4@example.com")
    show_id = await _show(session, tmdb_id=_next_id())
    special = await _episode(session, show_id, tmdb_id=None, season=3, number=-1)
    await _episode(session, show_id, tmdb_id=_next_id(), season=3, number=1)
    await _watched_episode(session, user.id, special)
    await session.commit()

    await repoint_episodes(session, min_ingested=_NO_FLOOR)

    assert special in await _episode_ids(session, show_id)
    assert await _watch_targets(session, user.id) == [special]


@pytest.mark.asyncio
async def test_two_copies_sharing_one_twin_are_refused(session, make_user):
    """The ambiguity production actually has — 443 keys, and the ticket predicted
    the other direction.

    Re-pointing both copies onto one twin merges two watch records into one, which
    `(user_id, episode_id)` would either reject or silently absorb. Both stay.
    """
    user = await make_user(email="er5@example.com")
    show_id = await _show(session, tmdb_id=_next_id())
    first = await _episode(session, show_id, tmdb_id=None)
    second = await _episode(session, show_id, tmdb_id=None)
    twin = await _episode(session, show_id, tmdb_id=_next_id())
    await _watched_episode(session, user.id, first)
    await _watched_episode(session, user.id, second)
    await session.commit()

    result = await repoint_episodes(session, min_ingested=_NO_FLOOR)

    assert result.episodes_deleted == 0
    assert await _episode_ids(session, show_id) == sorted([first, second, twin])
    assert await _watch_targets(session, user.id) == sorted([first, second])

    report = await build_report(session)
    assert report.kept_ambiguous_copies == 2
    assert report.repointable == 0


@pytest.mark.asyncio
async def test_two_ingested_twins_for_one_key_are_refused(session, make_user):
    """The ambiguity the ticket named. None in production, but representable —
    `catalog.episode` carries no `UNIQUE (show_id, season_number, episode_number)`
    — so it is refused rather than resolved by primary key."""
    user = await make_user(email="er6@example.com")
    show_id = await _show(session, tmdb_id=_next_id())
    copy = await _episode(session, show_id, tmdb_id=None)
    await _episode(session, show_id, tmdb_id=_next_id())
    await _episode(session, show_id, tmdb_id=_next_id())
    await _watched_episode(session, user.id, copy)
    await session.commit()

    result = await repoint_episodes(session, min_ingested=_NO_FLOOR)

    assert result.episodes_deleted == 0
    assert await _watch_targets(session, user.id) == [copy]
    assert (await build_report(session)).kept_ambiguous_twins == 1


@pytest.mark.asyncio
async def test_an_episode_under_a_locally_authored_show_is_untouched(session, make_user):
    """A show TMDB never matched has only copied episodes — they are not
    duplicates of anything, they are the only episode data it has."""
    user = await make_user(email="er7@example.com")
    show_id = await _show(session, tmdb_id=None)
    only = await _episode(session, show_id, tmdb_id=None)
    await _watched_episode(session, user.id, only)
    await session.commit()

    result = await repoint_episodes(session, min_ingested=_NO_FLOOR)

    assert result.episodes_deleted == 0
    assert await _episode_ids(session, show_id) == [only]
    assert await _watch_targets(session, user.id) == [only]


@pytest.mark.asyncio
async def test_a_user_holding_both_rows_keeps_the_copy(session, make_user):
    """The collision the three uniqueness constraints make representable.

    Moving this user's row onto the twin would merge two watch records into one,
    and the reconciliation harness would read the missing row as a loss. So the
    copy is kept with its row intact, and the pass says so.
    """
    user = await make_user(email="er8@example.com")
    show_id, copy, twin = await _pair(session)
    await _watched_episode(session, user.id, copy)
    await _watched_episode(session, user.id, twin)
    await session.commit()

    result = await repoint_episodes(session, min_ingested=_NO_FLOOR)

    assert result.blocked_by_collision == 1
    assert result.episodes_deleted == 0
    assert result.watches_repointed == 0
    assert await _watch_targets(session, user.id) == sorted([copy, twin])
    assert await _episode_ids(session, show_id) == sorted([copy, twin])


@pytest.mark.asyncio
async def test_a_collision_on_one_user_does_not_strand_another(session, make_user):
    """Two users on one copy, one of whom already holds the twin.

    The guard is per row, so the unblocked user's watch moves — but the copy has
    to stay for the blocked one, which is what stops the delete taking a row
    somebody still points at.
    """
    blocked = await make_user(email="er9a@example.com")
    movable = await make_user(email="er9b@example.com")
    _, copy, twin = await _pair(session)
    await _watched_episode(session, blocked.id, copy)
    await _watched_episode(session, blocked.id, twin)
    await _watched_episode(session, movable.id, copy)
    await session.commit()

    result = await repoint_episodes(session, min_ingested=_NO_FLOOR)

    assert result.watches_repointed == 1
    assert result.episodes_deleted == 0
    assert await _watch_targets(session, movable.id) == [twin]
    assert await _watch_targets(session, blocked.id) == sorted([copy, twin])


@pytest.mark.asyncio
async def test_activity_events_with_null_season_numbers_collide(session, make_user):
    """`uq_activity_event` is NULLS NOT DISTINCT, so two events with a null season
    number *do* conflict — a plain `=` in the guard would decide they do not and
    let the UPDATE raise, taking the batch with it."""
    user = await make_user(email="er10@example.com")
    _, copy, twin = await _pair(session)
    session.add(
        ActivityEvent(actor_id=user.id, verb="watched", target_type="episode", target_id=copy)
    )
    session.add(
        ActivityEvent(actor_id=user.id, verb="watched", target_type="episode", target_id=twin)
    )
    await session.commit()

    result = await repoint_episodes(session, min_ingested=_NO_FLOOR)

    assert result.activity_repointed == 0
    assert result.blocked_by_collision == 1


@pytest.mark.asyncio
async def test_a_show_event_is_never_mistaken_for_an_episode_one(session, make_user):
    """`target_id` is polymorphic, so a show event whose id happens to equal a
    copied episode's must not be dragged along by `target_type`-blind SQL."""
    user = await make_user(email="er11@example.com")
    _, copy, _twin = await _pair(session)
    session.add(ActivityEvent(actor_id=user.id, verb="added", target_type="show", target_id=copy))
    await session.commit()

    result = await repoint_episodes(session, min_ingested=_NO_FLOOR)

    assert result.activity_repointed == 0
    unchanged = (
        await session.execute(
            select(ActivityEvent.target_id)
            .where(ActivityEvent.actor_id == user.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert unchanged == copy


@pytest.mark.asyncio
async def test_the_pass_is_idempotent(session, make_user):
    """A row leaves the work list by being deleted, so a second run is a no-op."""
    user = await make_user(email="er12@example.com")
    _, copy, twin = await _pair(session)
    await _watched_episode(session, user.id, copy)
    await session.commit()

    first = await repoint_episodes(session, min_ingested=_NO_FLOOR)
    second = await repoint_episodes(session, min_ingested=_NO_FLOOR)

    assert first.episodes_deleted == 1
    assert (second.episodes_deleted, second.watches_repointed) == (0, 0)
    assert await _watch_targets(session, user.id) == [twin]


@pytest.mark.asyncio
async def test_limit_caps_the_run_and_the_rest_survives_a_re_run(session, make_user):
    """`--limit` is how a hundred get tried before the full pass is spent."""
    user = await make_user(email="er13@example.com")
    pairs = [await _pair(session, number=n) for n in (1, 2, 3)]
    for _show_id, copy, _twin in pairs:
        await _watched_episode(session, user.id, copy)
    await session.commit()

    first = await repoint_episodes(session, limit=2, batch_size=1, min_ingested=_NO_FLOOR)
    assert first.episodes_deleted == 2

    second = await repoint_episodes(session, min_ingested=_NO_FLOOR)
    assert second.episodes_deleted == 1
    assert await _watch_targets(session, user.id) == sorted(twin for _s, _c, twin in pairs)


@pytest.mark.asyncio
async def test_batching_walks_the_whole_work_list(session, make_user):
    """The keyset cursor has to advance past every batch — a cursor that stalled
    would spin, and one that overshot would silently skip rows."""
    user = await make_user(email="er14@example.com")
    pairs = [await _pair(session, number=n) for n in range(1, 8)]
    for _show_id, copy, _twin in pairs:
        await _watched_episode(session, user.id, copy)
    await session.commit()

    result = await repoint_episodes(session, batch_size=2, min_ingested=_NO_FLOOR)

    assert result.episodes_deleted == 7
    assert result.batches == 4
    assert await _watch_targets(session, user.id) == sorted(twin for _s, _c, twin in pairs)


@pytest.mark.asyncio
async def test_it_refuses_to_run_before_the_ingest(session):
    """Before the ingest almost no copy has a twin, so the pass would report a
    clean grain having moved nothing — and the report is what somebody decides
    on. `show_prune` draws the same line for the same reason."""
    await _pair(session)
    await session.commit()

    with pytest.raises(IngestNotRun):
        await repoint_episodes(session, min_ingested=150_000)


@pytest.mark.asyncio
async def test_the_report_counts_without_writing(session, make_user):
    """`report` runs against production before the pass is spent, so it must
    leave the database exactly as it found it."""
    user = await make_user(email="er15@example.com")
    show_id, copy, _twin = await _pair(session)
    await _watched_episode(session, user.id, copy)
    orphan_show = await _show(session, tmdb_id=_next_id())
    await _episode(session, orphan_show, tmdb_id=None, season=4, number=1)
    await session.commit()

    report = await build_report(session)

    assert report.repointable == 1
    assert report.watches_to_move == 1
    assert report.user_touched_repointable == 1
    assert report.kept_no_counterpart == 1
    assert await _episode_ids(session, show_id) == sorted([copy, _twin])
    assert await _watch_targets(session, user.id) == [copy]


@pytest.mark.asyncio
async def test_the_report_enumerates_what_stays_doubled(session, make_user):
    """`repointable` reaching zero says the pass has nothing left to do, not that
    the grain is clean. `still_doubled` is what scores the criterion, and it flags
    the pairs a person would actually care about."""
    user = await make_user(email="er16@example.com")
    show_id = await _show(session, tmdb_id=_next_id())
    first = await _episode(session, show_id, tmdb_id=None)
    await _episode(session, show_id, tmdb_id=None)
    await _episode(session, show_id, tmdb_id=_next_id())
    await _watched_episode(session, user.id, first)
    await session.commit()

    report = await build_report(session)

    (row,) = [r for r in report.still_doubled if r["show_id"] == show_id]
    assert row["rows"] == 3
    assert row["ingested_rows"] == 1
    assert row["carries_user_data"] is True


@pytest.mark.asyncio
async def test_reconciliation_counts_are_unchanged_by_the_re_point(session, make_user):
    """The ticket's first acceptance criterion, against the harness that scores it.

    Re-pointing moves a watch *within* a show, so the per-`(user, show)` count the
    reconciliation harness compares must come out identical — which is what lets
    NEU-1125 pass on the far side of this pass.
    """
    from tvbf.app.services import reconciliation_service as rs

    user = await make_user(email="er17@example.com")
    show_id, copy, twin = await _pair(session)
    await _watched_episode(session, user.id, copy)
    await session.commit()

    before = await rs.build_snapshot(session, spine="catalog")
    await repoint_episodes(session, min_ingested=_NO_FLOOR)
    after = await rs.build_snapshot(session, spine="catalog")

    assert rs.compare(before, after) == []
    assert await _watch_targets(session, user.id) == [twin]


@pytest.mark.asyncio
async def test_an_aborted_batch_rolls_back(session, make_user):
    """The abort exists because the cursor steps past a batch either way: a row
    the delete neither removed nor deliberately kept would be skipped silently."""
    user = await make_user(email="er18@example.com")
    # Read before the abort: its rollback expires every instance in the session,
    # so touching `user.id` afterwards would lazy-load outside the greenlet.
    user_id = user.id
    _, copy, _twin = await _pair(session)
    await _watched_episode(session, user_id, copy)
    await session.commit()

    from tvbf.tmdb import episode_repoint as module

    original = module._DELETE
    # A delete that matches nothing, leaving a row that is neither gone nor
    # blocked — exactly the disagreement the guard is there to catch.
    module._DELETE = module.text(
        "DELETE FROM catalog.episode e WHERE e.id = ANY(cast(:doomed AS bigint[])) AND false"
    )
    try:
        with pytest.raises(EpisodeRepointAborted):
            await repoint_episodes(session, min_ingested=_NO_FLOOR)
    finally:
        module._DELETE = original

    assert await _watch_targets(session, user_id) == [copy]


@pytest.mark.asyncio
async def test_one_persons_rows_move_together_or_not_at_all(session, make_user):
    """The cross-table split the per-`(episode, user)` refusal exists to prevent.

    This user's activity event collides while their watch does not. Guarding each
    write site independently would move the watch to the twin and leave the event
    on the copy, splitting one person's history across two episode rows — worse
    than either whole answer.
    """
    user = await make_user(email="er19@example.com")
    _, copy, twin = await _pair(session)
    await _watched_episode(session, user.id, copy)
    session.add(
        ActivityEvent(actor_id=user.id, verb="watched", target_type="episode", target_id=copy)
    )
    session.add(
        ActivityEvent(actor_id=user.id, verb="watched", target_type="episode", target_id=twin)
    )
    await session.commit()

    result = await repoint_episodes(session, min_ingested=_NO_FLOOR)

    # Neither moved, and the copy stayed to carry them.
    assert (result.watches_repointed, result.activity_repointed) == (0, 0)
    assert result.episodes_deleted == 0
    assert await _watch_targets(session, user.id) == [copy]


@pytest.mark.asyncio
async def test_the_pass_is_reversible_from_tvmaze(session, make_user):
    """The acceptance criterion, exercised rather than asserted in prose.

    Step one is `task copy:catalog`, which restores the deleted episode under its
    original id — and it restores seasons before episodes, so the `season_id` the
    copy carries forward has a parent to point at. Step two is the re-point in
    reverse, which `copy:catalog` cannot do because it never touches `app`.
    """
    from sqlalchemy import text as sql_text

    from tvbf.tvmaze.catalog_copy import copy_to_catalog
    from tvbf.tvmaze.models import Episode as MazeEpisode
    from tvbf.tvmaze.models import Show as MazeShow

    user = await make_user(email="er20@example.com")
    show_id, copy, twin = await _pair(session)
    # The copy's source rows, which are what a revert reads back from.
    session.add(MazeShow(id=show_id, name="Revertible", tvmaze_updated=1))
    await session.flush()
    session.add(MazeEpisode(id=copy, show_id=show_id, season=1, number=1))
    await _watched_episode(session, user.id, copy)
    await session.commit()

    await repoint_episodes(session, min_ingested=_NO_FLOOR)
    assert await _watch_targets(session, user.id) == [twin]

    await copy_to_catalog(session)
    await session.execute(
        sql_text("""
            UPDATE app.user_episode_watch w
               SET episode_id = c.id
              FROM catalog.episode t
              JOIN catalog.episode c
                ON c.show_id = t.show_id
               AND c.season_number = t.season_number
               AND c.episode_number = t.episode_number
               AND c.tmdb_id IS NULL
             WHERE t.id = w.episode_id
               AND t.tmdb_id IS NOT NULL
        """)
    )
    await session.commit()

    assert await _watch_targets(session, user.id) == [copy]
    assert copy in await _episode_ids(session, show_id)
