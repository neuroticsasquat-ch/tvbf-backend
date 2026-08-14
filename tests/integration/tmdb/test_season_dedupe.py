"""Season-grain deduplication (NEU-1119).

Every test here is one of the ticket's acceptance criteria or one of the ways the
pass could delete a row nothing can restore. The two that matter most are the
ones asserting what is *kept*: a copied season under a locally-authored show is
the only season data that show has, and `tvmaze` is the only place it could come
back from.

Seeding is doubled wherever a watch record is involved, for the reason
`test_episode_map.py` doubles it: `app.user_episode_watch` carries a foreign key
into `tvmaze.episode` while the rows being deduplicated live in `catalog`, and the
two share an id because NEU-1042 preserved TV Maze ids as the catalog surrogates.

No upstream is mocked because none is called — every question this pass asks is
answered in Postgres.
"""

import pytest
from sqlalchemy import select

from tvbf.app.models import UserEpisodeWatch
from tvbf.catalog import models as cm
from tvbf.tmdb.season_dedupe import (
    SeasonDedupeAborted,
    build_report,
    dedupe_seasons,
)

# Well clear of the browse fixtures' catalog, so every assertion can name exact ids.
_ID = 9_800_000


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


async def _show(session, *, tmdb_id: int | None, name: str = "Dedupe Show") -> int:
    """A `catalog.show`. `tmdb_id=None` is the locally-authored (unmatched) case."""
    show_id = _next_id()
    session.add(cm.Show(id=show_id, name=name, tmdb_id=tmdb_id))
    await session.flush()
    return show_id


async def _season(session, show_id: int, number: int, *, tmdb_id: int | None) -> int:
    """A season. `tmdb_id=None` is a copied row, set is one the ingest wrote."""
    season_id = _next_id()
    session.add(cm.Season(id=season_id, show_id=show_id, season_number=number, tmdb_id=tmdb_id))
    await session.flush()
    return season_id


async def _episode(session, show_id: int, season_id: int, number: int, *, season: int = 1) -> int:
    episode_id = _next_id()
    session.add(
        cm.Episode(
            id=episode_id,
            show_id=show_id,
            season_id=season_id,
            season_number=season,
            episode_number=number,
        )
    )
    await session.flush()
    return episode_id


async def _season_ids(session, show_id: int) -> list[int]:
    rows = await session.execute(
        select(cm.Season.id).where(cm.Season.show_id == show_id).order_by(cm.Season.id)
    )
    return list(rows.scalars())


@pytest.mark.asyncio
async def test_deletes_the_copy_and_keeps_the_ingested_row(session):
    """AC 1: no show carries two `catalog.season` rows for one season."""
    show_id = await _show(session, tmdb_id=1396)
    copied = await _season(session, show_id, 1, tmdb_id=None)
    ingested = await _season(session, show_id, 1, tmdb_id=3572)
    await session.commit()

    result = await dedupe_seasons(session)

    assert result.seasons_deleted == 1
    assert await _season_ids(session, show_id) == [ingested]
    assert copied != ingested


@pytest.mark.asyncio
async def test_episodes_move_to_the_surviving_season(session):
    """AC 3: no episode loses its `season_id`.

    The ticket assumed these were orphans. `upsert_episodes` re-points only the
    episodes it writes, and a copied episode carrying no `tmdb_id` is not one —
    production had 2,125,419 still attached, so a bare delete would trip
    `ON DELETE SET NULL` across every one of them.
    """
    show_id = await _show(session, tmdb_id=1396)
    copied = await _season(session, show_id, 1, tmdb_id=None)
    ingested = await _season(session, show_id, 1, tmdb_id=3572)
    episode_id = await _episode(session, show_id, copied, 1)
    await session.commit()

    result = await dedupe_seasons(session)

    assert result.episodes_repointed == 1
    episode = await session.get(cm.Episode, episode_id, populate_existing=True)
    assert episode is not None
    assert episode.season_id == ingested


@pytest.mark.asyncio
async def test_a_watched_episode_keeps_a_season_that_exists(session, make_user):
    """The same, said in terms of the thing the migration is protecting."""
    user = await make_user(email=f"sd{_next_id()}@example.com")
    show_id = _next_id()
    session.add(cm.Show(id=show_id, name="Watched", tmdb_id=1396))
    await session.flush()

    copied = await _season(session, show_id, 1, tmdb_id=None)
    ingested = await _season(session, show_id, 1, tmdb_id=3572)

    episode_id = _next_id()
    session.add(
        cm.Episode(
            id=episode_id,
            show_id=show_id,
            season_id=copied,
            season_number=1,
            episode_number=4,
        )
    )
    await session.flush()
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=episode_id))
    await session.commit()

    await dedupe_seasons(session)

    episode = await session.get(cm.Episode, episode_id, populate_existing=True)
    assert episode is not None
    assert episode.season_id == ingested
    watch = await session.execute(
        select(UserEpisodeWatch).where(UserEpisodeWatch.episode_id == episode_id)
    )
    assert watch.scalar_one() is not None


@pytest.mark.asyncio
async def test_season_under_a_locally_authored_show_is_untouched(session):
    """AC 2, and the one that costs data no feed can restore if it regresses."""
    show_id = await _show(session, tmdb_id=None, name="Unmatched")
    copied = await _season(session, show_id, 1, tmdb_id=None)
    await session.commit()

    result = await dedupe_seasons(session)

    assert result.seasons_deleted == 0
    assert await _season_ids(session, show_id) == [copied]


@pytest.mark.asyncio
async def test_copied_season_with_no_counterpart_is_kept(session):
    """A matched show whose TMDB payload has no season of that number.

    Not a duplicate — it is TV Maze data with nothing standing in for it, and
    deleting it is the failure `prune_missing_seasons`' `tmdb_id IS NOT NULL`
    guard exists to prevent one layer up.
    """
    show_id = await _show(session, tmdb_id=1396)
    orphan = await _season(session, show_id, 7, tmdb_id=None)
    ingested = await _season(session, show_id, 1, tmdb_id=3572)
    await session.commit()

    result = await dedupe_seasons(session)

    assert result.seasons_deleted == 0
    assert await _season_ids(session, show_id) == sorted([orphan, ingested])


@pytest.mark.asyncio
async def test_two_ingested_rows_for_one_number_are_refused(session):
    """Ambiguity is never resolved by primary key.

    Which of the two an episode belongs to has no answer, so the copied row stays
    and is counted. `catalog.season` carries no `UNIQUE (show_id, season_number)`,
    so the state is representable even though TMDB was measured not to produce it.
    """
    show_id = await _show(session, tmdb_id=1396)
    copied = await _season(session, show_id, 1, tmdb_id=None)
    first = await _season(session, show_id, 1, tmdb_id=3572)
    second = await _season(session, show_id, 1, tmdb_id=3573)
    await session.commit()

    result = await dedupe_seasons(session)

    assert result.seasons_deleted == 0
    assert await _season_ids(session, show_id) == sorted([copied, first, second])

    report = await build_report(session)
    assert report.ambiguous == 1
    assert report.deletable_duplicates == 0


@pytest.mark.asyncio
async def test_two_copied_rows_collapse_onto_the_one_survivor(session):
    """TV Maze numbers two seasons the same on 13 shows (NEU-1042).

    Where the show *did* match, both copies defer to the single ingested row and
    both sets of episodes land on it — which is the merge TMDB's own numbering
    already describes.
    """
    show_id = await _show(session, tmdb_id=1396)
    first_copy = await _season(session, show_id, 1, tmdb_id=None)
    second_copy = await _season(session, show_id, 1, tmdb_id=None)
    ingested = await _season(session, show_id, 1, tmdb_id=3572)
    a = await _episode(session, show_id, first_copy, 1)
    b = await _episode(session, show_id, second_copy, 2)
    await session.commit()

    result = await dedupe_seasons(session)

    assert result.seasons_deleted == 2
    assert await _season_ids(session, show_id) == [ingested]
    for episode_id in (a, b):
        episode = await session.get(cm.Episode, episode_id, populate_existing=True)
        assert episode is not None
        assert episode.season_id == ingested


@pytest.mark.asyncio
async def test_repoint_follows_the_season_not_the_episodes_own_number(session):
    """`catalog.episode.season_number` is denormalised and can already disagree.

    One production row does. The episode moves to the doomed season's replacement,
    which keeps the disagreement exactly where it was; re-homing it by its own
    number would be this pass adjudicating which field is right on evidence it
    does not have. Pinned so the choice stays a decision.
    """
    show_id = await _show(session, tmdb_id=1396)
    copied = await _season(session, show_id, 1, tmdb_id=None)
    ingested = await _season(session, show_id, 1, tmdb_id=3572)
    elsewhere = await _season(session, show_id, 2, tmdb_id=3573)
    # Sits on the season numbered 1 while claiming to be in season 2.
    episode_id = await _episode(session, show_id, copied, 1, season=2)
    await session.commit()

    await dedupe_seasons(session)

    episode = await session.get(cm.Episode, episode_id, populate_existing=True)
    assert episode is not None
    assert episode.season_id == ingested
    assert episode.season_id != elsewhere
    assert episode.season_number == 2


@pytest.mark.asyncio
async def test_is_idempotent(session):
    show_id = await _show(session, tmdb_id=1396)
    await _season(session, show_id, 1, tmdb_id=None)
    await _season(session, show_id, 1, tmdb_id=3572)
    await session.commit()

    assert (await dedupe_seasons(session)).seasons_deleted == 1
    assert (await dedupe_seasons(session)).seasons_deleted == 0


@pytest.mark.asyncio
async def test_limit_caps_the_run_and_the_rest_survives_a_re_run(session):
    show_id = await _show(session, tmdb_id=1396)
    for number in (1, 2, 3):
        await _season(session, show_id, number, tmdb_id=None)
        await _season(session, show_id, number, tmdb_id=3570 + number)
    await session.commit()

    assert (await dedupe_seasons(session, limit=2)).seasons_deleted == 2
    assert (await build_report(session)).deletable_duplicates == 1
    assert (await dedupe_seasons(session)).seasons_deleted == 1
    assert (await build_report(session)).deletable_duplicates == 0


@pytest.mark.asyncio
async def test_batches_are_separate_transactions(session):
    """A killed pass keeps whatever earlier batches committed."""
    show_id = await _show(session, tmdb_id=1396)
    for number in (1, 2, 3):
        await _season(session, show_id, number, tmdb_id=None)
        await _season(session, show_id, number, tmdb_id=3570 + number)
    await session.commit()

    result = await dedupe_seasons(session, batch_size=1)

    assert result.seasons_deleted == 3
    assert result.batches == 3


@pytest.mark.asyncio
async def test_report_counts_each_population(session):
    matched = await _show(session, tmdb_id=1396)
    duplicate = await _season(session, matched, 1, tmdb_id=None)
    await _season(session, matched, 1, tmdb_id=3572)
    await _season(session, matched, 9, tmdb_id=None)
    await _episode(session, matched, duplicate, 1)

    unmatched = await _show(session, tmdb_id=None, name="Unmatched")
    await _season(session, unmatched, 1, tmdb_id=None)
    await session.commit()

    report = await build_report(session)

    assert report.deletable_duplicates == 1
    assert report.episodes_to_repoint == 1
    assert report.kept_under_unmatched_show == 1
    assert report.kept_no_counterpart == 1
    assert report.ambiguous == 0


@pytest.mark.asyncio
async def test_report_counts_episodes_carrying_user_data(session, make_user):
    user = await make_user(email=f"sd{_next_id()}@example.com")
    show_id = _next_id()
    session.add(cm.Show(id=show_id, name="Counted", tmdb_id=1396))
    await session.flush()

    copied = await _season(session, show_id, 1, tmdb_id=None)
    await _season(session, show_id, 1, tmdb_id=3572)

    watched_id = _next_id()
    session.add(
        cm.Episode(
            id=watched_id, show_id=show_id, season_id=copied, season_number=1, episode_number=1
        )
    )
    await session.flush()
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=watched_id))
    # A second episode on the same doomed season that nobody has touched.
    await _episode(session, show_id, copied, 2)
    await session.commit()

    report = await build_report(session)

    assert report.episodes_to_repoint == 2
    assert report.episodes_carrying_user_data == 1


@pytest.mark.asyncio
async def test_report_names_every_pair_that_stays_doubled(session):
    """The residue of AC 1, which this pass does not fully reach.

    All three shapes belong in one place — only the first was obvious, and
    scoping the query to unmatched shows silently folded the other two into
    `kept_no_counterpart` and `ambiguous`, where nothing said the criterion was
    still unmet.
    """
    # 1. TV Maze's own duplicate numbering under a locally-authored show.
    unmatched = await _show(session, tmdb_id=None, name="Doubled and unmatched")
    await _season(session, unmatched, 2, tmdb_id=None)
    await _season(session, unmatched, 2, tmdb_id=None)

    # 2. The same under a matched show, on a number TMDB has no season for —
    #    neither row has a counterpart to defer to.
    no_counterpart = await _show(session, tmdb_id=1396, name="Doubled, no counterpart")
    await _season(session, no_counterpart, 5, tmdb_id=None)
    await _season(session, no_counterpart, 5, tmdb_id=None)

    # 3. Two rows the ingest itself wrote for one number.
    ambiguous = await _show(session, tmdb_id=1398, name="Two ingested")
    await _season(session, ambiguous, 1, tmdb_id=4001)
    await _season(session, ambiguous, 1, tmdb_id=4002)
    await session.commit()

    report = await build_report(session)

    by_show = {row["show_id"]: row for row in report.still_doubled}
    assert set(by_show) == {unmatched, no_counterpart, ambiguous}
    assert by_show[unmatched] == {
        "show_id": unmatched,
        "season_number": 2,
        "rows": 2,
        "ingested_rows": 0,
        "show_matched": False,
    }
    assert by_show[no_counterpart]["show_matched"] is True
    assert by_show[no_counterpart]["ingested_rows"] == 0
    assert by_show[ambiguous]["ingested_rows"] == 2
    assert report.deletable_duplicates == 0


@pytest.mark.asyncio
async def test_a_resolved_pair_leaves_the_residue_report(session):
    """`still_doubled` is a scoreboard, so a pair the pass fixes has to leave it."""
    show_id = await _show(session, tmdb_id=1396)
    await _season(session, show_id, 1, tmdb_id=None)
    await _season(session, show_id, 1, tmdb_id=3572)

    # Doomed rows are excluded before the run too — the report says what the
    # grain looks like *after* the pass, not before it.
    assert (await build_report(session)).still_doubled == ()

    await session.commit()
    await dedupe_seasons(session)

    assert (await build_report(session)).still_doubled == ()


@pytest.mark.asyncio
async def test_a_delete_that_matches_fewer_rows_aborts(session, monkeypatch):
    """The loop's only way to spin, closed.

    `_DELETE` re-derives `_SELECT_BATCH`'s predicates instead of trusting the id
    list, so if the two ever disagree the work list hands back the same rows for
    ever. Stop with the batch rolled back rather than loop.
    """
    show_id = await _show(session, tmdb_id=1396)
    copied = await _season(session, show_id, 1, tmdb_id=None)
    await _season(session, show_id, 1, tmdb_id=3572)
    await session.commit()

    from sqlalchemy import text as sql_text

    from tvbf.tmdb import season_dedupe

    # A delete that matches nothing, standing in for the two queries disagreeing.
    # Still binds `:doomed`, so the only thing that changes is how many rows match.
    monkeypatch.setattr(
        season_dedupe,
        "_DELETE",
        sql_text("DELETE FROM catalog.season WHERE id = ANY(cast(:doomed AS bigint[])) AND false"),
    )

    with pytest.raises(SeasonDedupeAborted):
        await dedupe_seasons(session)

    assert copied in await _season_ids(session, show_id)
