"""Show-grain pruning (NEU-1066).

Every test here is one of the ticket's acceptance criteria or one of the ways the
pass could delete a row nothing can restore. The ones that matter most are the
four asserting what is *kept*, because this pass is the only place in the
migration where a show disappears: a user's history, a person's verdict, a row
that was never a copy, and a matched row are each spared by a different predicate,
and `_DELETE` re-asserts all four rather than trusting the work list.

Seeding is doubled wherever a user row is involved, for the reason
`test_season_dedupe.py` doubles it: `app.user_episode_watch` carries a foreign key
into `tvmaze.episode` while the rows being pruned live in `catalog`, and the two
share an id because NEU-1042 preserved TV Maze ids as the catalog surrogates. The
`tvmaze.show` row is not incidental either — it is what makes a catalog row a
*copy*, which is one of the four predicates.

No upstream is mocked because none is called — every question this pass asks is
answered in Postgres.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql_text

from tvbf.app.models import (
    ActivityEvent,
    User,
    UserEpisodeRating,
    UserEpisodeWatch,
    UserShowRating,
    UserShowWatch,
)
from tvbf.catalog import models as cm
from tvbf.tmdb import show_prune
from tvbf.tmdb.show_prune import (
    IngestNotRun,
    ShowPruneAborted,
    build_report,
    prune_shows,
)
from tvbf.tvmaze.models import Episode as MazeEpisode
from tvbf.tvmaze.models import Show as MazeShow

# Well clear of the browse fixtures' catalog, so every assertion can name exact ids.
_ID = 9_700_000

# Every test seeds a handful of shows, not 150,000, so the floor guard has to come
# down or nothing below it runs. It gets its own test instead.
_NO_FLOOR = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


async def _copied_show(
    session,
    *,
    name: str = "Pruned Show",
    match_method: str | None = None,
    mirrored: bool = True,
) -> int:
    """An unmatched copied show: `catalog.show` with `tmdb_id IS NULL`.

    `mirrored=False` drops the `tvmaze.show` row, which is what a locally-authored
    row created *after* the migration looks like — same NULL `tmdb_id`, no copy
    behind it.
    """
    show_id = _next_id()
    if mirrored:
        session.add(MazeShow(id=show_id, name=name, tvmaze_updated=0))
        await session.flush()
    session.add(cm.Show(id=show_id, name=name, tmdb_id=None, match_method=match_method))
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


async def _episode(session, show_id: int, *, number: int = 1) -> int:
    """An episode in both spines under the same id, as the copy left them."""
    episode_id = _next_id()
    session.add(MazeEpisode(id=episode_id, show_id=show_id, season=1, number=number))
    session.add(
        cm.Episode(
            id=episode_id,
            show_id=show_id,
            season_number=1,
            episode_number=number,
        )
    )
    await session.flush()
    return episode_id


async def _user(session) -> uuid.UUID:
    user = User(
        id=uuid.uuid4(),
        email=f"prune-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="x",
        display_name="Pruner",
    )
    session.add(user)
    await session.flush()
    return user.id


async def _show_ids(session, ids: list[int]) -> list[int]:
    rows = await session.execute(select(cm.Show.id).where(cm.Show.id.in_(ids)).order_by(cm.Show.id))
    return list(rows.scalars())


@pytest.mark.asyncio
async def test_deletes_the_unmatched_untouched_copy(session):
    """AC: the duplicate half of a copied/ingested pair goes."""
    copied = await _copied_show(session, name="ITV News at Ten")
    ingested = await _ingested_show(session, tmdb_id=3679, name="ITV News at Ten")
    await session.commit()

    result = await prune_shows(session, min_ingested=_NO_FLOOR)

    assert result.shows_deleted == 1
    assert await _show_ids(session, [copied, ingested]) == [ingested]


@pytest.mark.asyncio
async def test_deletes_an_unmatched_copy_with_no_twin_at_all(session):
    """The rule is not "duplicates" — it is "unmatched and untouched".

    Three quarters of production's unmatched rows have no ingested row sharing
    their title. They are breadth from the source being retired, carried for
    nobody, and they go for the same reason the duplicates do.
    """
    copied = await _copied_show(session, name="Никому Не Известный Сериал")
    await session.commit()

    result = await prune_shows(session, min_ingested=_NO_FLOOR)

    assert result.shows_deleted == 1
    assert await _show_ids(session, [copied]) == []


@pytest.mark.asyncio
async def test_takes_its_seasons_and_episodes_with_it(session):
    """The spine cascades from `catalog.show`, and that is the intent."""
    copied = await _copied_show(session)
    season = cm.Season(id=_next_id(), show_id=copied, season_number=1, tmdb_id=None)
    session.add(season)
    await session.flush()
    episode = await _episode(session, copied)
    await session.commit()

    await prune_shows(session, min_ingested=_NO_FLOOR)

    seasons = await session.execute(select(cm.Season.id).where(cm.Season.show_id == copied))
    episodes = await session.execute(select(cm.Episode.id).where(cm.Episode.id == episode))
    assert list(seasons.scalars()) == []
    assert list(episodes.scalars()) == []
    # The TV Maze original is untouched — it is what `task copy:catalog` restores from.
    maze = await session.execute(select(MazeShow.id).where(MazeShow.id == copied))
    assert list(maze.scalars()) == [copied]


@pytest.mark.asyncio
async def test_keeps_a_show_on_someones_list(session):
    """AC: every show a user has touched stays reachable and fully functional."""
    copied = await _copied_show(session, name="Discretion")
    user_id = await _user(session)
    session.add(UserShowWatch(user_id=user_id, show_id=copied))
    await session.commit()

    result = await prune_shows(session, min_ingested=_NO_FLOOR)

    assert result.shows_deleted == 0
    assert await _show_ids(session, [copied]) == [copied]


@pytest.mark.asyncio
async def test_keeps_a_show_reached_only_through_an_episode_watch(session):
    """The show grain is not where all the history lives.

    A user can have watched episodes of a show that was never added to My Shows,
    and `app.user_episode_watch` reaches it only through `tvmaze.episode`.
    """
    copied = await _copied_show(session)
    episode_id = await _episode(session, copied)
    user_id = await _user(session)
    session.add(UserEpisodeWatch(user_id=user_id, episode_id=episode_id))
    await session.commit()

    result = await prune_shows(session, min_ingested=_NO_FLOOR)

    assert result.shows_deleted == 0
    assert await _show_ids(session, [copied]) == [copied]


@pytest.mark.asyncio
async def test_keeps_a_show_named_only_by_an_activity_event(session):
    """`app.activity_event` is polymorphic with no foreign key at all.

    It neither blocks the delete nor cascades — it silently orphans, which is the
    specific hazard ADR-0005 cites and the easiest of the five sites to forget.
    """
    copied = await _copied_show(session)
    user_id = await _user(session)
    session.add(
        ActivityEvent(
            actor_id=user_id,
            verb="watched_show",
            target_type="show",
            target_id=copied,
        )
    )
    await session.commit()

    result = await prune_shows(session, min_ingested=_NO_FLOOR)

    assert result.shows_deleted == 0
    assert await _show_ids(session, [copied]) == [copied]


@pytest.mark.asyncio
async def test_keeps_a_row_a_person_ruled_locally_authored(session):
    """`match_method='human'` with a NULL `tmdb_id` is a verdict, not an omission.

    Deleting it discards the review it records (NEU-1044) and puts the show back
    in the queue the next time anyone looks.
    """
    ruled = await _copied_show(session, match_method="human")
    await session.commit()

    result = await prune_shows(session, min_ingested=_NO_FLOOR)

    assert result.shows_deleted == 0
    assert await _show_ids(session, [ruled]) == [ruled]


@pytest.mark.asyncio
async def test_keeps_a_locally_authored_row_that_was_never_a_copy(session):
    """ "Delete the copy" has to mean the copy.

    A row authored after the migration reads `tmdb_id IS NULL` exactly like a
    copied one; the `tvmaze.show` existence test is what tells them apart, and it
    is also what makes `task copy:catalog` an exact revert.
    """
    authored = await _copied_show(session, mirrored=False)
    await session.commit()

    result = await prune_shows(session, min_ingested=_NO_FLOOR)

    assert result.shows_deleted == 0
    assert await _show_ids(session, [authored]) == [authored]


@pytest.mark.asyncio
async def test_keeps_every_matched_row(session):
    """A copied row that matched is not a copy any more — it *is* the show.

    Its id is the one `app.*` references and the one every existing URL uses.
    """
    matched = _next_id()
    session.add(MazeShow(id=matched, name="Matched", tvmaze_updated=0))
    await session.flush()
    session.add(cm.Show(id=matched, name="Matched", tmdb_id=1396, match_method="tvdb_id"))
    await session.commit()

    result = await prune_shows(session, min_ingested=_NO_FLOOR)

    assert result.shows_deleted == 0
    assert await _show_ids(session, [matched]) == [matched]


@pytest.mark.asyncio
async def test_refuses_to_run_before_the_ingest(session):
    """The floor guard, which is the one that stops an 89,025-row accident.

    Before `enrich:tmdb-ids`, no copied row holds a `tmdb_id` and the work list is
    the entire mirror. Reversible or not, that is not a mistake to make silently.
    """
    copied = await _copied_show(session)
    await session.commit()

    with pytest.raises(IngestNotRun):
        await prune_shows(session)

    assert await _show_ids(session, [copied]) == [copied]


@pytest.mark.asyncio
async def test_is_idempotent_and_resumable(session):
    """A row leaves the work list by being deleted, so a re-run costs nothing."""
    await _copied_show(session)
    await _copied_show(session)
    await session.commit()

    first = await prune_shows(session, min_ingested=_NO_FLOOR)
    second = await prune_shows(session, min_ingested=_NO_FLOOR)

    assert first.shows_deleted == 2
    assert second.shows_deleted == 0


@pytest.mark.asyncio
async def test_limit_stops_the_pass_early(session):
    """`--limit N` is how to try a hundred before spending the full pass."""
    await _copied_show(session)
    await _copied_show(session)
    await session.commit()

    result = await prune_shows(session, limit=1, min_ingested=_NO_FLOOR)

    assert result.shows_deleted == 1
    report = await build_report(session)
    assert report.deletable == 1


@pytest.mark.asyncio
async def test_batching_covers_every_show(session):
    """The batch loop must run the work list dry, not one batch of it."""
    for _ in range(5):
        await _copied_show(session)
    await session.commit()

    result = await prune_shows(session, batch_size=2, min_ingested=_NO_FLOOR)

    assert result.shows_deleted == 5
    assert result.batches == 3


@pytest.mark.asyncio
async def test_aborts_when_the_delete_disagrees_with_the_work_list(session, monkeypatch):
    """`_DELETE` re-derives `_DOOMED`'s predicates, so disagreement is a bug.

    The work list would hand the same rows back next time round, so the loop
    would spin rather than fail. It stops instead, with the batch rolled back.
    """
    copied = await _copied_show(session)
    await session.commit()

    # A delete that matches nothing, standing in for the two queries diverging.
    monkeypatch.setattr(
        show_prune,
        "_DELETE",
        sql_text("DELETE FROM catalog.show WHERE FALSE AND id = ANY(cast(:doomed AS bigint[]))"),
    )

    with pytest.raises(ShowPruneAborted):
        await prune_shows(session, min_ingested=_NO_FLOOR)

    assert await _show_ids(session, [copied]) == [copied]


@pytest.mark.asyncio
async def test_report_counts_and_enumerates_without_writing(session):
    """The report is what decides whether to spend the pass, so it writes nothing."""
    copied = await _copied_show(session, name="Doomed")
    session.add(cm.Season(id=_next_id(), show_id=copied, season_number=1, tmdb_id=None))
    await session.flush()
    await _episode(session, copied)

    touched = await _copied_show(session, name="Discretion")
    user_id = await _user(session)
    session.add(UserShowWatch(user_id=user_id, show_id=touched))
    ruled = await _copied_show(session, match_method="human", name="Ruled")
    authored = await _copied_show(session, mirrored=False, name="Authored")
    await session.commit()

    report = await build_report(session)

    assert report.deletable == 1
    assert report.seasons_to_delete == 1
    assert report.episodes_to_delete == 1
    assert report.kept_user_touched == 1
    assert report.kept_human_verdict == 1
    assert report.kept_not_copied == 1
    assert [row["show_id"] for row in report.user_touched] == [touched]
    assert report.user_touched[0]["tracked"] is True
    # Nothing was deleted by reporting.
    assert await _show_ids(session, [copied, touched, ruled, authored]) == sorted(
        [copied, touched, ruled, authored]
    )


@pytest.mark.asyncio
async def test_still_doubled_names_a_spared_duplicate(session):
    """The acceptance criterion is "no show appears twice", not "work list empty".

    A kept row sharing a title with an ingested one is exactly where those two
    diverge, so the report names it — in production that is the user-touched
    residue and nothing else.
    """
    touched = await _copied_show(session, name="Discretion")
    ingested = await _ingested_show(session, tmdb_id=300966, name="Discretion")
    user_id = await _user(session)
    session.add(UserShowWatch(user_id=user_id, show_id=touched))
    await session.commit()

    report = await build_report(session)

    assert [row["show_id"] for row in report.still_doubled] == [touched]
    assert report.still_doubled[0]["ingested_ids"] == [ingested]


@pytest.mark.asyncio
async def test_still_doubled_folds_the_title_the_way_search_does(session):
    """The comparison goes through `sql_fold.folded`, not a second definition.

    A Python-side `unaccent` does not decompose ł, ø, đ or ħ, so re-spelling the
    fold here would disagree with browse search on precisely the titles the fold
    exists for.
    """
    touched = await _copied_show(session, name="Wałęsa: Człowiek z Nadziei")
    ingested = await _ingested_show(session, tmdb_id=555_001, name="Walesa - Czlowiek z Nadziei")
    user_id = await _user(session)
    session.add(UserShowWatch(user_id=user_id, show_id=touched))
    await session.commit()

    report = await build_report(session)

    assert [row["show_id"] for row in report.still_doubled] == [touched]
    assert report.still_doubled[0]["ingested_ids"] == [ingested]


@pytest.mark.asyncio
async def test_still_doubled_ignores_a_title_that_folds_away(session):
    """ "!!!" and "???" fold to the same empty string and are not the same show."""
    touched = await _copied_show(session, name="!!!")
    await _ingested_show(session, tmdb_id=555_002, name="???")
    user_id = await _user(session)
    session.add(UserShowWatch(user_id=user_id, show_id=touched))
    await session.commit()

    report = await build_report(session)

    assert report.still_doubled == ()


@pytest.mark.asyncio
async def test_keeps_a_show_reached_only_by_a_show_rating(session):
    """Rating a show without adding it is history too."""
    copied = await _copied_show(session)
    user_id = await _user(session)
    session.add(UserShowRating(user_id=user_id, show_id=copied, stars=Decimal("4.0")))
    await session.commit()

    result = await prune_shows(session, min_ingested=_NO_FLOOR)

    assert result.shows_deleted == 0
    assert await _show_ids(session, [copied]) == [copied]


@pytest.mark.asyncio
async def test_keeps_a_show_reached_only_by_an_episode_rating(session):
    """The fourth site, and the one with no show-grain row to notice it."""
    copied = await _copied_show(session)
    episode_id = await _episode(session, copied)
    user_id = await _user(session)
    session.add(UserEpisodeRating(user_id=user_id, episode_id=episode_id, stars=Decimal("4.5")))
    await session.commit()

    result = await prune_shows(session, min_ingested=_NO_FLOOR)

    assert result.shows_deleted == 0
    assert await _show_ids(session, [copied]) == [copied]


@pytest.mark.asyncio
async def test_keeps_a_show_named_only_by_an_episode_target_activity_event(session):
    """The branch the ticket singles out: *"widen to episode-target events too"*.

    An `activity_event` with `target_type='episode'` names an episode id, so the
    show is only reachable by joining through `tvmaze.episode` — and with no
    foreign key anywhere, nothing but this predicate would stop the delete.
    """
    copied = await _copied_show(session)
    episode_id = await _episode(session, copied)
    user_id = await _user(session)
    session.add(
        ActivityEvent(
            actor_id=user_id,
            verb="watched_episode",
            target_type="episode",
            target_id=episode_id,
        )
    )
    await session.commit()

    result = await prune_shows(session, min_ingested=_NO_FLOOR)

    assert result.shows_deleted == 0
    assert await _show_ids(session, [copied]) == [copied]


@pytest.mark.asyncio
async def test_report_splits_the_doomed_rows_by_whether_tmdb_has_them(session):
    """AC 1: *"how many are genuinely absent from TMDB rather than merely unmatched"*.

    The partition counts say how many rows go; this says which of them are
    duplicates the matcher missed and which are breadth TMDB never had.
    """
    await _copied_show(session, name="ITV News at Ten")
    await _ingested_show(session, tmdb_id=3679, name="ITV News at Ten")
    await _copied_show(session, name="Никому Не Известный Сериал")
    await session.commit()

    report = await build_report(session)

    assert report.deletable == 2
    assert report.deletable_with_title_twin == 1
    assert report.deletable_without_title_twin == 1
