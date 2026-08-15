"""Retiring the TV Maze orphan rows (NEU-1146).

Every test here is one of the ticket's acceptance criteria or one of the ways
this pass could cost somebody their watch history — a bigger surface than
`episode_repoint`'s, because this pass deletes catalog rows that have **no**
counterpart and takes the user rows on them with it. That is the migration's
former absolute constraint being deliberately reversed, so the tests that matter
most are the ones pinning *what is deleted* and *what the report said would be*.

The three named production cases each have a test built to their measured shape:
Will & Grace (a split series, rescued by the season offset and **not** by title —
`s9e1` is "Eleven Years Later" against TMDB's "11 Years Later"), Cunk on Earth
(an orphan show TMDB models as a season of an anthology, whose air dates all
disagree), and the two-parter collision (one viewing keeps one row).

No upstream is mocked because none is called — every question this pass asks is
answered in Postgres.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from tvbf.app.models import (
    ActivityEvent,
    UserEpisodeRating,
    UserEpisodeWatch,
    UserShowRating,
    UserShowWatch,
)
from tvbf.catalog import models as cm
from tvbf.tmdb.orphan_retire import (
    LOSS_BASIS_MATCHED_TWIN,
    LOSS_BASIS_POSITION_ONLY,
    LOSS_DEDUPLICATION,
    LOSS_GENUINE,
    TIER_EXACT_KEY,
    TIER_LINK_OFFSET_KEY,
    TIER_LINK_TITLE,
    TIER_SAME_SHOW_TITLE,
    IngestNotRun,
    build_report,
    retire_orphans,
)

# Well clear of the browse fixtures' catalog, so every assertion can name exact ids.
_ID = 9_920_000

# Every test seeds a handful of shows, not 150,000, so the ingest floor has to
# come down or nothing below it runs. It gets its own test instead.
_NO_FLOOR = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


async def _show(session, *, tmdb_id: int | None, name: str = "Retire Show") -> int:
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


async def _season(session, show_id: int, *, tmdb_id: int | None, number: int = 1) -> int:
    season_id = _next_id()
    session.add(cm.Season(id=season_id, show_id=show_id, season_number=number, tmdb_id=tmdb_id))
    await session.flush()
    return season_id


async def _episode(
    session,
    show_id: int,
    *,
    tmdb_id: int | None,
    season: int = 1,
    number: int = 1,
    name: str | None = None,
    air_date: date | None = None,
    season_id: int | None = None,
) -> int:
    episode_id = _next_id()
    session.add(
        cm.Episode(
            id=episode_id,
            show_id=show_id,
            season_id=season_id,
            season_number=season,
            episode_number=number,
            name=name,
            air_date=air_date,
            tmdb_id=tmdb_id,
        )
    )
    await session.flush()
    return episode_id


async def _watch(session, user_id, episode_id: int) -> None:
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


async def _tracked_shows(session, user_id) -> list[int]:
    rows = await session.execute(
        select(UserShowWatch.show_id)
        .where(UserShowWatch.user_id == user_id)
        .order_by(UserShowWatch.show_id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars())


async def _episode_ids(session, show_id: int) -> list[int]:
    rows = await session.execute(
        select(cm.Episode.id).where(cm.Episode.show_id == show_id).order_by(cm.Episode.id)
    )
    return list(rows.scalars())


async def _orphan_counts(session) -> tuple[int, int, int]:
    """Criterion 7's query: how many `tmdb_id IS NULL` rows survive at each grain."""
    episodes = await session.execute(select(cm.Episode.id).where(cm.Episode.tmdb_id.is_(None)))
    seasons = await session.execute(select(cm.Season.id).where(cm.Season.tmdb_id.is_(None)))
    shows = await session.execute(select(cm.Show.id).where(cm.Show.tmdb_id.is_(None)))
    return (
        len(list(episodes.scalars())),
        len(list(seasons.scalars())),
        len(list(shows.scalars())),
    )


# --------------------------------------------------------------------------
# Tier 0 — the exact key, which is not spent: deltas keep making new ones.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_exact_key_still_pairs_and_the_watch_moves(session, make_user):
    user = await make_user(email="or1@example.com")
    show = await _show(session, tmdb_id=_next_id())
    orphan = await _episode(session, show, tmdb_id=None, name="Pilot")
    twin = await _episode(session, show, tmdb_id=_next_id(), name="Pilot")
    await _watch(session, user.id, orphan)

    report = await build_report(session, min_ingested=_NO_FLOOR)
    assert report.by_tier[TIER_EXACT_KEY] == 1

    result = await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert await _watch_targets(session, user.id) == [twin]
    assert await _episode_ids(session, show) == [twin]
    assert result.watches_moved == 1
    assert result.watches_deleted == 0


# --------------------------------------------------------------------------
# Tier 1 — the folded title within the show. The air date is deliberately not
# consulted; §2.4 measured 34 correct matches with a non-zero date delta.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_unique_title_pairs_even_when_the_air_dates_disagree(session, make_user):
    """SNL's "The Best of John Belushi": TV Maze has the 1998 broadcast, TMDB the 2005 release."""
    user = await make_user(email="or2@example.com")
    show = await _show(session, tmdb_id=_next_id())
    orphan = await _episode(
        session,
        show,
        tmdb_id=None,
        season=3,
        number=7,
        name="The Best of John Belushi",
        air_date=date(1998, 7, 1),
    )
    twin = await _episode(
        session,
        show,
        tmdb_id=_next_id(),
        season=0,
        number=4,
        name="The Best of John Belushi",
        air_date=date(2005, 8, 30),
    )
    await _watch(session, user.id, orphan)

    await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert await _watch_targets(session, user.id) == [twin]
    assert await _episode_ids(session, show) == [twin]


@pytest.mark.asyncio
async def test_punctuation_and_case_fold_away_but_a_spelled_out_numeral_does_not(session):
    """The fold's reach, pinned: it is why tier 2 must not consult the title."""
    show = await _show(session, tmdb_id=_next_id())
    orphan = await _episode(
        session, show, tmdb_id=None, season=9, number=3, name="Missing Pieces 13: So It Begins"
    )
    twin = await _episode(
        session,
        show,
        tmdb_id=_next_id(),
        season=9,
        number=9,
        name="Missing Pieces (13): So It Begins",
    )
    await _episode(session, show, tmdb_id=None, season=9, number=4, name="Eleven Years Later")
    await _episode(session, show, tmdb_id=_next_id(), season=9, number=10, name="11 Years Later")

    report = await build_report(session, min_ingested=_NO_FLOOR)

    assert report.by_tier[TIER_SAME_SHOW_TITLE] == 1
    assert report.to_delete == 1
    await retire_orphans(session, min_ingested=_NO_FLOOR)
    assert twin in await _episode_ids(session, show)
    assert orphan not in await _episode_ids(session, show)


@pytest.mark.asyncio
async def test_two_orphans_sharing_a_title_are_both_refused(session, make_user):
    """Ambiguity on the orphan side resolves to unmatched, never by primary key."""
    user = await make_user(email="or3@example.com")
    show = await _show(session, tmdb_id=_next_id())
    first = await _episode(session, show, tmdb_id=None, season=1, number=5, name="Reunion")
    second = await _episode(session, show, tmdb_id=None, season=2, number=5, name="Reunion")
    await _episode(session, show, tmdb_id=_next_id(), season=3, number=1, name="Reunion")
    await _watch(session, user.id, first)

    report = await build_report(session, min_ingested=_NO_FLOOR)
    assert report.by_tier[TIER_SAME_SHOW_TITLE] == 0
    assert report.rejections["ambiguous_two_or_more_orphans_share_the_title"] == 2

    await retire_orphans(session, min_ingested=_NO_FLOOR)
    # Both deleted, as criterion 7 requires — but neither was silently paired.
    assert first not in await _episode_ids(session, show)
    assert second not in await _episode_ids(session, show)
    assert await _watch_targets(session, user.id) == []


@pytest.mark.asyncio
async def test_two_ingested_episodes_sharing_a_title_are_refused(session):
    show = await _show(session, tmdb_id=_next_id())
    await _episode(session, show, tmdb_id=None, season=1, number=9, name="Homecoming")
    await _episode(session, show, tmdb_id=_next_id(), season=1, number=1, name="Homecoming")
    await _episode(session, show, tmdb_id=_next_id(), season=2, number=1, name="Homecoming")

    report = await build_report(session, min_ingested=_NO_FLOOR)

    assert report.by_tier[TIER_SAME_SHOW_TITLE] == 0
    assert report.rejections["ambiguous_two_or_more_ingested_share_the_title"] == 1


@pytest.mark.asyncio
async def test_a_title_that_folds_to_nothing_never_matches(session):
    """ "!!!" and "???" fold to the same empty string — the one way this could pair strangers."""
    show = await _show(session, tmdb_id=_next_id())
    await _episode(session, show, tmdb_id=None, season=1, number=3, name="!!!")
    await _episode(session, show, tmdb_id=_next_id(), season=1, number=1, name="???")

    report = await build_report(session, min_ingested=_NO_FLOOR)

    assert report.by_tier[TIER_SAME_SHOW_TITLE] == 0
    assert report.rejections["blank_title_after_folding"] == 1


# --------------------------------------------------------------------------
# Tier 2 — the split series. Will & Grace's measured shape.
# --------------------------------------------------------------------------


async def _will_and_grace(session):
    """The original, its revival as a separate TMDB series, and the season-8 offset."""
    original = await _show(session, tmdb_id=_next_id(), name="Will & Grace")
    revival = await _show(session, tmdb_id=_next_id(), name="Will & Grace")
    # The original's own ingested run, so the show is genuinely matched.
    await _episode(
        session, original, tmdb_id=_next_id(), season=1, number=1, name="Live and Let Dry"
    )
    # The evidence pair: identical folded title *and* exact air date.
    await _episode(
        session,
        original,
        tmdb_id=None,
        season=9,
        number=2,
        name="Who's Your Daddy",
        air_date=date(2017, 10, 5),
    )
    await _episode(
        session,
        revival,
        tmdb_id=_next_id(),
        season=1,
        number=2,
        name="Who's Your Daddy",
        air_date=date(2017, 10, 5),
    )
    # The premiere the title would lose: "Eleven Years Later" vs "11 Years Later".
    premiere_orphan = await _episode(
        session,
        original,
        tmdb_id=None,
        season=9,
        number=1,
        name="Eleven Years Later",
        air_date=date(2017, 9, 28),
    )
    premiere_twin = await _episode(
        session,
        revival,
        tmdb_id=_next_id(),
        season=1,
        number=1,
        name="11 Years Later",
        air_date=date(2017, 9, 28),
    )
    return original, revival, premiere_orphan, premiere_twin


@pytest.mark.asyncio
async def test_the_season_offset_rescues_the_premiere_a_title_rule_would_drop(session, make_user):
    """Criterion 6: a run that rescues only 16 of 17 has fallen back to title matching."""
    user = await make_user(email="or4@example.com")
    original, revival, orphan, twin = await _will_and_grace(session)
    await _watch(session, user.id, orphan)

    report = await build_report(session, min_ingested=_NO_FLOOR)
    assert report.by_tier[TIER_LINK_OFFSET_KEY] == 2
    assert [link["season_offset"] for link in report.links] == [8]
    assert [link["user_touched"] for link in report.links] == [1]

    await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert await _watch_targets(session, user.id) == [twin]
    assert await _episode_ids(session, original) != []
    assert orphan not in await _episode_ids(session, original)


@pytest.mark.asyncio
async def test_history_moving_to_another_show_gets_that_show_tracked(session, make_user):
    """§4.3 — intact by row count and invisible in the product without this."""
    user = await make_user(email="or5@example.com")
    original, revival, orphan, twin = await _will_and_grace(session)
    session.add(UserShowWatch(user_id=user.id, show_id=original))
    await _watch(session, user.id, orphan)
    await session.flush()

    report = await build_report(session, min_ingested=_NO_FLOOR)
    assert report.show_watches_to_create == 1

    result = await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert result.show_watches_created == 1
    assert revival in await _tracked_shows(session, user.id)


@pytest.mark.asyncio
async def test_a_show_with_two_link_candidates_links_to_neither(session):
    """More than one candidate is a refusal, not a tie to be broken."""
    original = await _show(session, tmdb_id=_next_id(), name="Lost")
    for _ in range(2):
        sibling = await _show(session, tmdb_id=_next_id(), name="Lost")
        await _episode(
            session,
            sibling,
            tmdb_id=_next_id(),
            season=1,
            number=1,
            name="Exodus",
            air_date=date(2005, 5, 25),
        )
    await _episode(session, original, tmdb_id=_next_id(), season=1, number=2, name="Pilot")
    await _episode(
        session,
        original,
        tmdb_id=None,
        season=7,
        number=1,
        name="Exodus",
        air_date=date(2005, 5, 25),
    )

    report = await build_report(session, min_ingested=_NO_FLOOR)

    assert report.links == ()
    assert report.links_dropped_multiple_candidates == 1
    assert report.by_tier[TIER_LINK_OFFSET_KEY] == 0


# --------------------------------------------------------------------------
# Tier 2b — the orphan show TMDB models as a season of an anthology.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_orphan_show_links_on_aggregate_episode_titles_and_is_retired(session, make_user):
    """Cunk on Earth: no same-folded-name sibling, and every air date disagrees."""
    user = await make_user(email="or6@example.com")
    orphan_show = await _show(session, tmdb_id=None, name="Cunk on Earth")
    anthology = await _show(session, tmdb_id=_next_id(), name="Cunk on…")
    season = await _season(session, orphan_show, tmdb_id=None, number=1)

    titles = [
        "In the Beginning",
        "Faith Off",
        "The Renaissance Will Not Be Televised",
        "Rise of the Machines",
        "War s What Is It Good For",
    ]
    orphans = []
    twins = []
    for index, title in enumerate(titles, start=1):
        orphans.append(
            await _episode(
                session,
                orphan_show,
                tmdb_id=None,
                season=1,
                number=index,
                name=title,
                air_date=date(2023, 1, 25),
                season_id=season,
            )
        )
        twins.append(
            await _episode(
                session,
                anthology,
                tmdb_id=_next_id(),
                season=2,
                number=index,
                name=title,
                air_date=date(2023, 1, 25 + index),
            )
        )
    session.add(UserShowWatch(user_id=user.id, show_id=orphan_show))
    session.add(UserShowRating(user_id=user.id, show_id=orphan_show, stars=Decimal("4.0")))
    await _watch(session, user.id, orphans[0])

    report = await build_report(session, min_ingested=_NO_FLOOR)
    assert report.by_tier[TIER_LINK_TITLE] == 5

    result = await retire_orphans(session, min_ingested=_NO_FLOOR)

    # The episode history moved, the show tracking moved, and nothing survives.
    assert await _watch_targets(session, user.id) == [twins[0]]
    assert await _tracked_shows(session, user.id) == [anthology]
    assert result.show_ratings_moved == 1
    assert result.shows_deleted == 1
    assert await _orphan_counts(session) == (0, 0, 0)


# --------------------------------------------------------------------------
# §4.2 — the reversal: a collision drops the redundant row rather than keeping it.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_user_holding_both_rows_keeps_one_and_the_copy_still_goes(session, make_user):
    """NEU-1126 kept the copy here. One viewing keeps one row, and that is deliberate."""
    user = await make_user(email="or7@example.com")
    show = await _show(session, tmdb_id=_next_id())
    orphan = await _episode(session, show, tmdb_id=None, name="Finale")
    twin = await _episode(session, show, tmdb_id=_next_id(), name="Finale")
    await _watch(session, user.id, orphan)
    await _watch(session, user.id, twin)

    result = await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert await _watch_targets(session, user.id) == [twin]
    assert await _episode_ids(session, show) == [twin]
    assert result.watches_moved == 0
    assert result.watches_deleted == 1


@pytest.mark.asyncio
async def test_a_colliding_activity_event_does_not_strand_the_watch(session, make_user):
    """The three write sites are independent; a collision on one must not block another."""
    user = await make_user(email="or8@example.com")
    show = await _show(session, tmdb_id=_next_id())
    orphan = await _episode(session, show, tmdb_id=None, name="Crossover")
    twin = await _episode(session, show, tmdb_id=_next_id(), name="Crossover")
    await _watch(session, user.id, orphan)
    for target in (orphan, twin):
        session.add(
            ActivityEvent(
                actor_id=user.id, verb="watched_episode", target_type="episode", target_id=target
            )
        )
    await session.flush()

    await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert await _watch_targets(session, user.id) == [twin]
    events = await session.execute(
        select(ActivityEvent.target_id)
        .where(ActivityEvent.actor_id == user.id)
        .execution_options(populate_existing=True)
    )
    assert list(events.scalars()) == [twin]


# --------------------------------------------------------------------------
# Tier 3 — the accepted loss, and the report that has to predict it exactly.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_orphan_with_no_counterpart_is_deleted_with_its_user_rows(session, make_user):
    """The reversal of the migration's absolute constraint, pinned."""
    user = await make_user(email="or9@example.com")
    show = await _show(session, tmdb_id=_next_id(), name="Saturday Night Live")
    await _episode(session, show, tmdb_id=_next_id(), season=1, number=1, name="Episode 1")
    orphan = await _episode(
        session,
        show,
        tmdb_id=None,
        season=40,
        number=-1,
        name="The Best of Will Ferrell, Volume 1",
        air_date=date(2002, 11, 5),
    )
    await _watch(session, user.id, orphan)
    session.add(UserEpisodeRating(user_id=user.id, episode_id=orphan, stars=Decimal("4.5")))
    await session.flush()

    report = await build_report(session, min_ingested=_NO_FLOOR)
    assert report.to_delete_user_touched == 1
    assert report.loss_summary == {LOSS_GENUINE: 2}
    assert [loss["episode_name"] for loss in report.losses] == [
        "The Best of Will Ferrell, Volume 1",
        "The Best of Will Ferrell, Volume 1",
    ]
    assert {loss["row_kind"] for loss in report.losses} == {"watch", "rating"}

    result = await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert await _watch_targets(session, user.id) == []
    assert result.watches_deleted == 1
    assert result.ratings_deleted == 1


@pytest.mark.asyncio
async def test_without_air_dates_a_collapse_is_reported_as_a_loss_it_may_not_be(session, make_user):
    """Both loss bases in one case, and the conservative half of the same-day rule.

    `Part 1` sits on the ingested episode's exact key, so the matcher pairs them
    and the collision that follows is **proven** redundant — no date is
    consulted. `Part 2` has no twin, so its verdict rests on which ingested
    episode occupies its position, and with no air date on either side there is
    no evidence that one broadcast became one row. The inference is refused and
    it reads as a genuine loss, even though this *is* the two-parter shape.

    That asymmetry is deliberate and runs the safe way: telling a reviewer
    something was lost when it was not costs them a look at `app.watch_archive`,
    where the converse tells them nothing was lost when it was — before a pass
    that cannot be undone.

    The dated version, where the inference succeeds, is
    `test_a_two_parter_sharing_its_air_date_is_still_a_deduplication`.
    """
    user = await make_user(email="or10@example.com")
    show = await _show(session, tmdb_id=_next_id(), name="Friends")
    kept = await _episode(
        session, show, tmdb_id=_next_id(), season=6, number=24, name="The One With the Proposal"
    )
    part_one = await _episode(
        session,
        show,
        tmdb_id=None,
        season=6,
        number=24,
        name="The One With the Proposal, Part 1",
    )
    part_two = await _episode(
        session,
        show,
        tmdb_id=None,
        season=6,
        number=25,
        name="The One With the Proposal, Part 2",
    )
    await _watch(session, user.id, kept)
    await _watch(session, user.id, part_one)
    await _watch(session, user.id, part_two)

    report = await build_report(session, min_ingested=_NO_FLOOR)

    # §6's asymmetry is gone in the way that matters: the original probe called
    # `Part 1` a de-duplication and `Part 2` a loss on the strength of which
    # adjacent number it happened to check. Here the two verdicts differ for a
    # reason the report states — one is proven, the other inferred and refused.
    assert report.loss_summary == {LOSS_DEDUPLICATION: 1, LOSS_GENUINE: 1}
    assert {loss["basis"] for loss in report.losses} == {
        LOSS_BASIS_MATCHED_TWIN,
        LOSS_BASIS_POSITION_ONLY,
    }
    proven = next(x for x in report.losses if x["basis"] == LOSS_BASIS_MATCHED_TWIN)
    assert proven["episode_number"] == 24 and proven["absorbed_by_episode_id"] == kept

    await retire_orphans(session, min_ingested=_NO_FLOOR)
    assert await _watch_targets(session, user.id) == [kept]
    assert part_one not in await _episode_ids(session, show)
    assert part_two not in await _episode_ids(session, show)


@pytest.mark.asyncio
async def test_the_report_predicts_exactly_what_the_pass_deletes(session, make_user):
    """Criterion 4: the loss must match the report's list, with no unlisted row."""
    user = await make_user(email="or11@example.com")
    show = await _show(session, tmdb_id=_next_id())
    await _episode(session, show, tmdb_id=_next_id(), season=1, number=1, name="One")
    moved = await _episode(session, show, tmdb_id=_next_id(), season=1, number=2, name="Two")
    orphan_matched = await _episode(session, show, tmdb_id=None, season=5, number=9, name="Two")
    orphan_lost = await _episode(
        session, show, tmdb_id=None, season=5, number=-1, name="Behind the Scenes"
    )
    await _watch(session, user.id, orphan_matched)
    await _watch(session, user.id, orphan_lost)

    report = await build_report(session, min_ingested=_NO_FLOOR)
    result = await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert report.watch_rows_to_move == result.watches_moved == 1
    assert report.watch_rows_to_delete == result.watches_deleted == 1
    assert len(report.losses) == 1
    assert await _watch_targets(session, user.id) == [moved]


# --------------------------------------------------------------------------
# Criterion 7, and the guards that keep the pass from reaching TMDB data.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_orphan_row_survives_at_any_grain(session):
    """Criterion 7 — the whole point, and what the frontend half waits on."""
    matched = await _show(session, tmdb_id=_next_id())
    orphan_season = await _season(session, matched, tmdb_id=None, number=3)
    await _episode(
        session, matched, tmdb_id=None, season=3, number=1, name="Gone", season_id=orphan_season
    )
    await _episode(session, matched, tmdb_id=_next_id(), season=1, number=1, name="Stays")
    await _season(session, matched, tmdb_id=_next_id(), number=1)
    unmatched = await _show(session, tmdb_id=None, name="Nowhere In TMDB")
    await _season(session, unmatched, tmdb_id=None, number=1)
    await _episode(session, unmatched, tmdb_id=None, season=1, number=1, name="Also Gone")

    await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert await _orphan_counts(session) == (0, 0, 0)


@pytest.mark.asyncio
async def test_an_ingested_episode_is_never_deleted(session):
    """`e.tmdb_id IS NULL` on the DELETE is what makes this structural."""
    show = await _show(session, tmdb_id=_next_id())
    survivors = [
        await _episode(session, show, tmdb_id=_next_id(), season=1, number=n, name=f"E{n}")
        for n in range(1, 4)
    ]
    await _episode(session, show, tmdb_id=None, season=9, number=1, name="Unpairable")

    await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert await _episode_ids(session, show) == sorted(survivors)


@pytest.mark.asyncio
async def test_an_orphan_season_holding_ingested_episodes_repoints_rather_than_orphaning_them(
    session,
):
    """§4.4's exception — two such seasons in production, and `SET NULL` is the hazard."""
    show = await _show(session, tmdb_id=_next_id())
    doomed = await _season(session, show, tmdb_id=None, number=2)
    survivor = await _season(session, show, tmdb_id=_next_id(), number=2)
    stranded = await _episode(
        session, show, tmdb_id=_next_id(), season=2, number=1, name="Ingested", season_id=doomed
    )

    result = await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert result.episodes_repointed_to_ingested_season == 1
    assert result.episodes_left_without_season == 0
    row = await session.get(cm.Episode, stranded, populate_existing=True)
    assert row is not None and row.season_id == survivor
    assert await _orphan_counts(session) == (0, 0, 0)


@pytest.mark.asyncio
async def test_it_refuses_to_run_before_the_full_ingest(session):
    """Almost no orphan has a counterpart pre-ingest, so the pass would delete the lot."""
    show = await _show(session, tmdb_id=_next_id())
    orphan = await _episode(session, show, tmdb_id=None, name="Pilot")

    with pytest.raises(IngestNotRun):
        await retire_orphans(session)

    assert orphan in await _episode_ids(session, show)


@pytest.mark.asyncio
async def test_a_limit_stops_at_a_show_boundary_and_leaves_the_later_grains_alone(session):
    """A season is only deletable once its episodes are gone, which a partial pass has not done.

    The limit rounds **up** to a whole show rather than cutting one short: a show
    is one transaction and one link resolution. So a limit of 1 against a show
    carrying three orphans retires all three, and stops before the next show.
    """
    first = await _show(session, tmdb_id=_next_id(), name="First Show")
    second = await _show(session, tmdb_id=_next_id(), name="Second Show")
    await _season(session, first, tmdb_id=None, number=1)
    for n in range(1, 4):
        await _episode(session, first, tmdb_id=None, season=9, number=n, name=f"Orphan {n}")
    survivor = await _episode(session, second, tmdb_id=None, season=9, number=1, name="Untouched")

    result = await retire_orphans(session, limit=1, min_ingested=_NO_FLOOR)

    assert result.episodes_deleted == 3
    assert result.seasons_deleted == 0
    assert result.shows_deleted == 0
    assert survivor in await _episode_ids(session, second)
    episodes, seasons, _ = await _orphan_counts(session)
    assert episodes == 1 and seasons == 1


@pytest.mark.asyncio
async def test_re_running_a_finished_pass_changes_nothing(session, make_user):
    """Idempotent: a row leaves the work list by being re-pointed or deleted."""
    user = await make_user(email="or12@example.com")
    show = await _show(session, tmdb_id=_next_id())
    orphan = await _episode(session, show, tmdb_id=None, name="Pilot")
    twin = await _episode(session, show, tmdb_id=_next_id(), name="Pilot")
    await _watch(session, user.id, orphan)

    await retire_orphans(session, min_ingested=_NO_FLOOR)
    second = await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert second.episodes_deleted == 0
    assert second.watches_deleted == 0
    assert await _watch_targets(session, user.id) == [twin]


# --------------------------------------------------------------------------
# The three gaps the NEU-1146 review found: a twin claimed twice, a link the
# report never listed, and a show-grain row deleted without appearing as a loss.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_orphans_never_claim_one_twin_across_tiers(session, make_user):
    """Uniqueness holds *within* a tier for free; across tiers it has to be enforced.

    The key-matched orphan wins the twin and the title-matched one falls to
    tier 3. Without the claim guard both re-point onto the same row, the second
    user row collides on `(user_id, episode_id)`, and a watch record is lost to
    an ambiguity §3 says must resolve to unmatched — with the report having
    predicted both would move.
    """
    user = await make_user(email="or13@example.com")
    show = await _show(session, tmdb_id=_next_id())
    twin = await _episode(session, show, tmdb_id=_next_id(), season=1, number=1, name="Pilot")
    by_key = await _episode(session, show, tmdb_id=None, season=1, number=1, name="Something Else")
    by_title = await _episode(session, show, tmdb_id=None, season=6, number=4, name="Pilot")
    await _watch(session, user.id, by_key)
    await _watch(session, user.id, by_title)

    report = await build_report(session, min_ingested=_NO_FLOOR)
    assert report.by_tier[TIER_EXACT_KEY] == 1
    assert report.by_tier[TIER_SAME_SHOW_TITLE] == 0
    assert report.to_delete == 1

    result = await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert await _watch_targets(session, user.id) == [twin]
    # The report said one moves and one is deleted, and that is what happened.
    assert report.watch_rows_to_move == result.watches_moved == 1
    assert report.watch_rows_to_delete == result.watches_deleted == 1
    assert len(report.losses) == 1


@pytest.mark.asyncio
async def test_an_orphan_show_with_no_episodes_still_appears_in_the_links(session, make_user):
    """Discretion's shape — a link the acceptance criteria name, carrying zero episodes."""
    user = await make_user(email="or14@example.com")
    orphan_show = await _show(session, tmdb_id=None, name="Discretion")
    sibling = await _show(session, tmdb_id=_next_id(), name="Discretion")
    await _episode(session, sibling, tmdb_id=_next_id(), season=1, number=1, name="Only Episode")
    session.add(UserShowWatch(user_id=user.id, show_id=orphan_show))
    await session.flush()

    report = await build_report(session, min_ingested=_NO_FLOOR)

    assert [(link["from_show_id"], link["to_show_id"]) for link in report.links] == [
        (orphan_show, sibling)
    ]
    assert report.links[0]["episodes_moved"] == 0

    result = await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert result.show_watches_moved == 1
    assert await _tracked_shows(session, user.id) == [sibling]
    assert await _orphan_counts(session) == (0, 0, 0)


@pytest.mark.asyncio
async def test_a_show_row_the_link_cannot_move_is_reported_as_a_loss(session, make_user):
    """Already tracking the destination means the orphan's row is dropped, not moved."""
    user = await make_user(email="or15@example.com")
    orphan_show = await _show(session, tmdb_id=None, name="Discretion")
    sibling = await _show(session, tmdb_id=_next_id(), name="Discretion")
    await _episode(session, sibling, tmdb_id=_next_id(), season=1, number=1, name="Only Episode")
    session.add(UserShowWatch(user_id=user.id, show_id=orphan_show))
    session.add(UserShowWatch(user_id=user.id, show_id=sibling))
    await session.flush()

    report = await build_report(session, min_ingested=_NO_FLOOR)

    assert report.loss_summary == {LOSS_DEDUPLICATION: 1}
    assert [loss["row_kind"] for loss in report.losses] == ["show_watch"]

    result = await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert result.show_watches_moved == 0
    assert result.show_watches_deleted == 1
    assert await _tracked_shows(session, user.id) == [sibling]


@pytest.mark.asyncio
async def test_an_orphan_show_referenced_by_import_staging_is_kept_and_named(session):
    """`import_ne.show_resolution` is NO ACTION, and its rows are an audit trail.

    Skipped rather than cascaded over or silently rewritten — and reported, since
    it is the one way a completed run can still leave criterion 7 unmet.
    """
    await session.execute(text("CREATE SCHEMA IF NOT EXISTS import_ne"))
    await session.execute(
        text(
            "CREATE TABLE IF NOT EXISTS import_ne.show_resolution ("
            "  id bigserial PRIMARY KEY,"
            "  show_id bigint REFERENCES catalog.show(id))"
        )
    )
    orphan_show = await _show(session, tmdb_id=None, name="Referenced By Staging")
    await session.execute(
        text("INSERT INTO import_ne.show_resolution (show_id) VALUES (:show_id)"),
        {"show_id": orphan_show},
    )
    await session.flush()

    result = await retire_orphans(session, min_ingested=_NO_FLOOR)

    assert result.shows_deleted == 0
    assert result.shows_kept_referenced == (orphan_show,)
    await session.execute(text("DROP TABLE import_ne.show_resolution"))
    await session.commit()


@pytest.mark.asyncio
async def test_a_special_does_not_poison_the_season_offset(session, make_user):
    """Production's Will & Grace shape: 47 pairs at offset 8, one special at 11.

    NEU-1042 numbered a TV Maze special negative *within its original season*
    while TMDB parks specials in season 0, so a special's season relationship is
    the one that does not follow the series'. Letting it into the offset made a
    unanimous 8 look inconsistent, collapsed the offset to `None`, and dropped
    the whole show to the title fallback — which rescues 16 of the 17
    user-touched rows and is named in the acceptance criteria as a failure.
    """
    user = await make_user(email="or16@example.com")
    original, revival, orphan, twin = await _will_and_grace(session)
    # The poison pair: a copied special in season 11 whose title and air date
    # match a TMDB special in season 0. Offset 11, against the series' 8.
    await _episode(
        session,
        original,
        tmdb_id=None,
        season=11,
        number=-1,
        name="Behind the Scenes",
        air_date=date(2020, 4, 23),
    )
    await _episode(
        session,
        revival,
        tmdb_id=_next_id(),
        season=0,
        number=1,
        name="Behind the Scenes",
        air_date=date(2020, 4, 23),
    )
    await _watch(session, user.id, orphan)

    report = await build_report(session, min_ingested=_NO_FLOOR)

    assert [link["season_offset"] for link in report.links] == [8]
    # The premiere moves on the offset key, not the title fallback — which is
    # the whole point, since its title does not fold equal to the twin's.
    assert report.by_tier_user_touched[TIER_LINK_OFFSET_KEY] == 1
    assert report.by_tier_user_touched[TIER_LINK_TITLE] == 0

    await retire_orphans(session, min_ingested=_NO_FLOOR)
    assert await _watch_targets(session, user.id) == [twin]


@pytest.mark.asyncio
async def test_an_orphan_past_the_end_of_the_season_is_a_loss_not_a_deduplication(
    session, make_user
):
    """The Hook Up Plan's shape, found on the first production report run.

    TMDB's season ends at 6; the orphan is a lockdown special from ten months
    later. The episode occupying its position is an unrelated finale the user
    watched independently, so calling it "absorbed" told the reviewer nothing was
    lost when something was. A merged two-parter airs in one slot, so the
    absorbing row carries the orphan's air date — that is what separates the two.
    """
    user = await make_user(email="or17@example.com")
    show = await _show(session, tmdb_id=_next_id(), name="The Hook Up Plan")
    finale = await _episode(
        session,
        show,
        tmdb_id=_next_id(),
        season=2,
        number=6,
        name="The Love Plan",
        air_date=date(2019, 10, 11),
    )
    orphan = await _episode(
        session,
        show,
        tmdb_id=None,
        season=2,
        number=7,
        name="Plan Confines",
        air_date=date(2020, 8, 26),
    )
    await _watch(session, user.id, finale)
    await _watch(session, user.id, orphan)

    report = await build_report(session, min_ingested=_NO_FLOOR)

    assert report.loss_summary == {LOSS_GENUINE: 1}
    assert [loss["basis"] for loss in report.losses] == [LOSS_BASIS_POSITION_ONLY]


@pytest.mark.asyncio
async def test_a_two_parter_sharing_its_air_date_is_still_a_deduplication(session, make_user):
    """The other side of the same rule: one broadcast really did become one row."""
    user = await make_user(email="or18@example.com")
    show = await _show(session, tmdb_id=_next_id(), name="Friends")
    merged = await _episode(
        session,
        show,
        tmdb_id=_next_id(),
        season=6,
        number=23,
        name="The One with the Proposal",
        air_date=date(2000, 5, 18),
    )
    for number, title in ((24, "The One With the Proposal, Part 1"), (25, "Part 2")):
        orphan = await _episode(
            session,
            show,
            tmdb_id=None,
            season=6,
            number=number,
            name=title,
            air_date=date(2000, 5, 18),
        )
        await _watch(session, user.id, orphan)
    await _watch(session, user.id, merged)

    report = await build_report(session, min_ingested=_NO_FLOOR)

    assert report.loss_summary == {LOSS_DEDUPLICATION: 2}
    assert {loss["basis"] for loss in report.losses} == {LOSS_BASIS_POSITION_ONLY}


@pytest.mark.asyncio
async def test_a_collision_on_a_matched_twin_is_proven_not_inferred(session, make_user):
    """Shrinking's shape: the matcher paired the rows, so no date test applies.

    Its TMDB and TV Maze air dates differ by a day (NEU-1145), and a blanket
    "dates must agree" rule would have turned a proven redundancy into a reported
    loss.
    """
    user = await make_user(email="or19@example.com")
    show = await _show(session, tmdb_id=_next_id(), name="Shrinking")
    twin = await _episode(
        session,
        show,
        tmdb_id=_next_id(),
        season=3,
        number=11,
        name="And That's Our Time",
        air_date=date(2026, 4, 7),
    )
    orphan = await _episode(
        session,
        show,
        tmdb_id=None,
        season=3,
        number=12,
        name="And That's Our Time",
        air_date=date(2026, 4, 8),
    )
    await _watch(session, user.id, twin)
    await _watch(session, user.id, orphan)

    report = await build_report(session, min_ingested=_NO_FLOOR)

    assert report.by_tier[TIER_SAME_SHOW_TITLE] == 1
    assert report.loss_summary == {LOSS_DEDUPLICATION: 1}
    assert [loss["basis"] for loss in report.losses] == [LOSS_BASIS_MATCHED_TWIN]
