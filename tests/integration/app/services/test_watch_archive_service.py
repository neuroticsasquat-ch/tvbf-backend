"""Service-level tests for watch_archive_service (NEU-1029)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from tests.fixtures.spines import mirror_spine
from tvbf.app.models import (
    UserEpisodeRating,
    UserEpisodeWatch,
    UserShowRating,
    UserShowWatch,
    WatchArchive,
)
from tvbf.app.services import watch_archive_service
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(
    session,
    *,
    show_id: int = 9100001,
    name: str = "Archive Show",
    premiered: date | None = date(2009, 3, 8),
) -> Show:
    show = Show(
        id=show_id,
        name=name,
        tvmaze_updated=1,
        premiered=premiered,
        externals_imdb="tt0944947",
        externals_tvdb=121361,
    )
    session.add(show)
    await session.flush()
    await mirror_spine(session)
    return show


async def _seed_episode(
    session,
    *,
    show_id: int,
    episode_id: int,
    season: int = 1,
    number: int | None = 1,
    name: str | None = "Winter Is Coming",
    airdate: date | None = date(2011, 4, 17),
) -> Episode:
    ep = Episode(
        id=episode_id,
        show_id=show_id,
        season=season,
        number=number,
        name=name,
        airdate=airdate,
    )
    session.add(ep)
    await session.flush()
    await mirror_spine(session)
    return ep


async def _rows(session, record_type: str) -> list[WatchArchive]:
    result = await session.execute(
        select(WatchArchive)
        .where(WatchArchive.record_type == record_type)
        .order_by(WatchArchive.id)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars())


@pytest.mark.asyncio
async def test_snapshot_archives_every_source_row(session, make_user):
    user = await make_user(email="wa1@example.com", display_name="Archivist")
    show = await _seed_show(session, show_id=9100101)
    ep = await _seed_episode(session, show_id=show.id, episode_id=9100201)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    session.add(
        UserEpisodeWatch(
            user_id=user.id,
            episode_id=ep.id,
            watched_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
        )
    )
    session.add(UserShowRating(user_id=user.id, show_id=show.id, stars=Decimal("4.5")))
    session.add(UserEpisodeRating(user_id=user.id, episode_id=ep.id, stars=Decimal("3.0")))
    await session.commit()

    result = await watch_archive_service.snapshot(session)

    assert result.source_total == 4
    assert result.inserted_total == 4
    assert result.archived_total == 4
    for record_type in ("show_watch", "episode_watch", "show_rating", "episode_rating"):
        counts = result.counts[record_type]
        assert counts.source == 1
        assert counts.archived == 1


@pytest.mark.asyncio
async def test_snapshot_records_human_readable_identity(session, make_user):
    """The archive has to describe what was watched without any catalog join."""
    user = await make_user(email="wa2@example.com", display_name="Archivist Two")
    show = await _seed_show(
        session, show_id=9100102, name="Game of Thrones", premiered=date(2011, 4, 17)
    )
    ep = await _seed_episode(
        session,
        show_id=show.id,
        episode_id=9100202,
        season=2,
        number=9,
        name="Blackwater",
        airdate=date(2012, 5, 27),
    )
    session.add(
        UserEpisodeWatch(
            user_id=user.id,
            episode_id=ep.id,
            watched_at=datetime(2026, 2, 3, 4, 5, tzinfo=UTC),
        )
    )
    await session.commit()

    await watch_archive_service.snapshot(session)

    (row,) = await _rows(session, "episode_watch")
    assert row.user_id == user.id
    assert row.user_email == "wa2@example.com"
    assert row.user_display_name == "Archivist Two"
    assert row.show_name == "Game of Thrones"
    assert row.show_premiered_year == 2011
    assert row.season_number == 2
    assert row.episode_number == 9
    assert row.episode_title == "Blackwater"
    assert row.episode_airdate == date(2012, 5, 27)
    assert row.occurred_at == datetime(2026, 2, 3, 4, 5, tzinfo=UTC)
    assert row.stars is None
    # TV Maze ids ride along as a convenience cross-reference, not as identity.
    assert row.source_show_id == show.id
    assert row.source_episode_id == ep.id
    assert row.show_imdb_id == "tt0944947"
    assert row.show_tvdb_id == 121361


@pytest.mark.asyncio
async def test_snapshot_records_ratings_with_stars(session, make_user):
    user = await make_user(email="wa3@example.com")
    show = await _seed_show(session, show_id=9100103)
    session.add(UserShowRating(user_id=user.id, show_id=show.id, stars=Decimal("4.5")))
    await session.commit()

    await watch_archive_service.snapshot(session)

    (row,) = await _rows(session, "show_rating")
    assert row.stars == Decimal("4.5")
    assert row.source_episode_id is None
    assert row.season_number is None


@pytest.mark.asyncio
async def test_snapshot_is_idempotent(session, make_user):
    """Re-running adds nothing and rewrites nothing — the archive is append-only."""
    user = await make_user(email="wa4@example.com")
    show = await _seed_show(session, show_id=9100104)
    ep = await _seed_episode(session, show_id=show.id, episode_id=9100204)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=ep.id))
    await session.commit()

    first = await watch_archive_service.snapshot(session)
    (original,) = await _rows(session, "episode_watch")
    original_archived_at = original.archived_at

    second = await watch_archive_service.snapshot(session)

    assert first.inserted_total == 2
    assert second.inserted_total == 0
    assert second.archived_total == 2

    (row,) = await _rows(session, "episode_watch")
    assert row.id == original.id
    assert row.archived_at == original_archived_at


@pytest.mark.asyncio
async def test_snapshot_picks_up_rows_added_since_the_last_run(session, make_user):
    user = await make_user(email="wa5@example.com")
    show = await _seed_show(session, show_id=9100105)
    first_ep = await _seed_episode(session, show_id=show.id, episode_id=9100205)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=first_ep.id))
    await session.commit()

    await watch_archive_service.snapshot(session)

    later_ep = await _seed_episode(
        session, show_id=show.id, episode_id=9100206, number=2, name="The Kingsroad"
    )
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=later_ep.id))
    await session.commit()

    second = await watch_archive_service.snapshot(session)

    assert second.counts["episode_watch"].inserted == 1
    assert second.counts["episode_watch"].archived == 2


@pytest.mark.asyncio
async def test_snapshot_keeps_the_original_row_when_a_watch_is_redone(session, make_user):
    """Unwatch-then-rewatch must not overwrite what the archive already holds."""
    user = await make_user(email="wa6@example.com")
    show = await _seed_show(session, show_id=9100106)
    ep = await _seed_episode(session, show_id=show.id, episode_id=9100207)
    session.add(
        UserEpisodeWatch(
            user_id=user.id,
            episode_id=ep.id,
            watched_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    await session.commit()
    await watch_archive_service.snapshot(session)

    await session.execute(
        text("DELETE FROM app.user_episode_watch WHERE user_id = :u"),
        {"u": user.id},
    )
    session.add(
        UserEpisodeWatch(
            user_id=user.id,
            episode_id=ep.id,
            watched_at=datetime(2026, 6, 30, tzinfo=UTC),
        )
    )
    await session.commit()

    await watch_archive_service.snapshot(session)

    rows = await _rows(session, "episode_watch")
    assert len(rows) == 1
    assert rows[0].occurred_at == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_snapshot_keeps_users_and_shows_apart(session, make_user):
    """The dedupe key is per source row, not per show or per user."""
    one = await make_user(email="wa7a@example.com")
    two = await make_user(email="wa7b@example.com")
    show_a = await _seed_show(session, show_id=9100107, name="Show A")
    show_b = await _seed_show(session, show_id=9100108, name="Show B")
    for user in (one, two):
        for show in (show_a, show_b):
            session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()

    result = await watch_archive_service.snapshot(session)

    assert result.counts["show_watch"].archived == 4
    assert len(await _rows(session, "show_watch")) == 4


@pytest.mark.asyncio
async def test_snapshot_tolerates_a_show_without_a_premiere_date(session, make_user):
    user = await make_user(email="wa8@example.com")
    show = await _seed_show(session, show_id=9100109, premiered=None)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()

    await watch_archive_service.snapshot(session)

    (row,) = await _rows(session, "show_watch")
    assert row.show_premiered_year is None
    assert row.show_name == "Archive Show"


@pytest.mark.asyncio
async def test_snapshot_tolerates_an_unnumbered_episode(session, make_user):
    """TV Maze leaves 27k specials unnumbered; the title carries the identity."""
    user = await make_user(email="wa9@example.com")
    show = await _seed_show(session, show_id=9100110)
    ep = await _seed_episode(
        session,
        show_id=show.id,
        episode_id=9100210,
        season=0,
        number=None,
        name="A Special",
    )
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=ep.id))
    await session.commit()

    await watch_archive_service.snapshot(session)

    (row,) = await _rows(session, "episode_watch")
    assert row.episode_number is None
    assert row.episode_title == "A Special"
    assert row.season_number == 0


@pytest.mark.asyncio
async def test_snapshot_survives_dropping_the_catalog_reference(session, make_user):
    """The archive answers the reconstruction question with no catalog join.

    This is the whole point of the table: the query below touches `app` only.
    """
    user = await make_user(email="wa10@example.com", display_name="Reader")
    show = await _seed_show(session, show_id=9100111, name="Deep Space Nine")
    ep = await _seed_episode(
        session, show_id=show.id, episode_id=9100211, season=6, number=13, name="Far Beyond"
    )
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=ep.id))
    await session.commit()
    await watch_archive_service.snapshot(session)

    reconstructed = (
        await session.execute(
            text(
                "SELECT show_name, season_number, episode_number, episode_title "
                "FROM app.watch_archive "
                "WHERE user_email = :email AND record_type = 'episode_watch'"
            ),
            {"email": "wa10@example.com"},
        )
    ).all()

    assert reconstructed == [("Deep Space Nine", 6, 13, "Far Beyond")]


@pytest.mark.asyncio
async def test_snapshot_raises_when_a_source_row_is_unarchived(session, make_user, monkeypatch):
    """A snapshot that silently covers part of the history would be trusted."""
    user = await make_user(email="wa11@example.com")
    show = await _seed_show(session, show_id=9100112)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()

    async def _one_missing(db, build_select) -> int:
        return 1

    monkeypatch.setattr(watch_archive_service, "_unarchived_count", _one_missing)

    with pytest.raises(watch_archive_service.ArchiveIncomplete) as excinfo:
        await watch_archive_service.snapshot(session)

    assert excinfo.value.unarchived["show_watch"] == 1
    # The failed run rolled back rather than leaving a partial archive behind.
    archived = (await session.execute(select(func.count()).select_from(WatchArchive))).scalar_one()
    assert archived == 0


@pytest.mark.asyncio
async def test_verification_catches_a_gap_a_count_comparison_would_miss(session, make_user):
    """`archived >= source` passes here; only the anti-join sees the gap.

    Archive an episode, delete its watch, watch a different one: the totals
    balance at 1 vs 1 while the second episode is genuinely unarchived.
    """
    user = await make_user(email="wa13@example.com")
    show = await _seed_show(session, show_id=9100114)
    first = await _seed_episode(session, show_id=show.id, episode_id=9100214)
    second = await _seed_episode(
        session, show_id=show.id, episode_id=9100215, number=2, name="The Kingsroad"
    )
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=first.id))
    await session.commit()
    await watch_archive_service.snapshot(session)

    await session.execute(
        text("DELETE FROM app.user_episode_watch WHERE episode_id = :e"), {"e": first.id}
    )
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=second.id))
    await session.commit()

    # Before the second run: one source row, one archive row — and a real gap.
    assert (
        await watch_archive_service._unarchived_count(
            session, watch_archive_service._select_episode_watches
        )
        == 1
    )

    result = await watch_archive_service.snapshot(session)

    counts = result.counts["episode_watch"]
    assert counts.source == 1
    assert counts.archived == 2  # the first episode's row is still there
    assert counts.unarchived == 0


@pytest.mark.asyncio
async def test_deleting_a_user_leaves_their_archive_rows_standing(session, make_user):
    """ "Never pruned" has no account-deletion exception — the reconciliation
    harness has to count the same rows either side of cutover."""
    user = await make_user(email="wa12@example.com")
    show = await _seed_show(session, show_id=9100113)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()
    await watch_archive_service.snapshot(session)

    await session.execute(text("DELETE FROM app.user WHERE id = :u"), {"u": user.id})
    await session.commit()

    (row,) = await _rows(session, "show_watch")
    assert row.user_id == user.id
    assert row.user_email == "wa12@example.com"


@pytest.mark.asyncio
async def test_archive_rows_cannot_be_updated(session, make_user):
    """Append-only is enforced by the table, not by how the writer behaves."""
    user = await make_user(email="wa14@example.com")
    show = await _seed_show(session, show_id=9100115)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()
    await watch_archive_service.snapshot(session)

    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(text("UPDATE app.watch_archive SET show_name = 'Tampered'"))
    await session.rollback()


@pytest.mark.asyncio
async def test_archive_rows_cannot_be_deleted(session, make_user):
    user = await make_user(email="wa15@example.com")
    show = await _seed_show(session, show_id=9100116)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()
    await watch_archive_service.snapshot(session)

    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(text("DELETE FROM app.watch_archive"))
    await session.rollback()
