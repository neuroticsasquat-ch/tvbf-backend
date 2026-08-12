"""Specials do not count toward progress — NEU-1062's whole point.

A user who has watched every regular episode of a show and none of its specials
saw something less than 100%. These tests hold the fix at the level a user
experiences it: My Shows, Watched, Watch Next, Upcoming, per-season progress and
bulk marking, rather than at the query.

The three-shape fixture is the same one the ledger uses — a real season carrying
a copied negative special, plus a TMDB-native season 0 — because both kinds have
to be excluded and only one of them is TMDB's idea.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from tvbf.app.errors import NotFound
from tvbf.app.models import UserShowWatch
from tvbf.app.repos import episode_watch_repo
from tvbf.app.services import episode_service, my_shows_service
from tvbf.catalog.models import Episode, Show

TODAY = date(2026, 8, 12)
AIRED = TODAY - timedelta(days=30)
FUTURE = TODAY + timedelta(days=30)


async def _seed(
    session,
    *,
    show_id: int,
    regulars: int = 2,
    copied_specials: int = 1,
    native_specials: int = 2,
    status: str = "Ended",
) -> Show:
    """A show with `regulars` episodes in season 1, `copied_specials` negative
    numbers inside that same season, and `native_specials` in season 0."""
    show = Show(id=show_id, name=f"Show-{show_id}", status=status)
    session.add(show)
    await session.flush()
    ep_id = show_id * 10
    for n in range(1, regulars + 1):
        ep_id += 1
        session.add(
            Episode(id=ep_id, show_id=show_id, season_number=1, episode_number=n, air_date=AIRED)
        )
    for n in range(1, copied_specials + 1):
        ep_id += 1
        session.add(
            Episode(id=ep_id, show_id=show_id, season_number=1, episode_number=-n, air_date=AIRED)
        )
    for n in range(1, native_specials + 1):
        ep_id += 1
        session.add(
            Episode(id=ep_id, show_id=show_id, season_number=0, episode_number=n, air_date=AIRED)
        )
    await session.flush()
    return show


async def _episodes(session, show_id: int) -> list[Episode]:
    from sqlalchemy import select

    return list(
        (await session.execute(select(Episode).where(Episode.show_id == show_id))).scalars().all()
    )


def _regular_ids(episodes: list[Episode]) -> list[int]:
    return [e.id for e in episodes if e.season_number != 0 and e.episode_number >= 0]


def _special_ids(episodes: list[Episode]) -> list[int]:
    return [e.id for e in episodes if e.season_number == 0 or e.episode_number < 0]


async def _mark(session, user_id, episode_ids: list[int]) -> None:
    await episode_watch_repo.bulk_mark(
        session,
        user_id=user_id,
        episode_ids=episode_ids,
        watched_at=datetime.now(UTC),
    )
    await session.flush()


# ---------------------------------------------------------------------------
# The acceptance case
# ---------------------------------------------------------------------------


async def test_every_regular_episode_watched_and_no_specials_reads_one_hundred_percent(
    session, make_user
):
    user = await make_user()
    show = await _seed(session, show_id=964_100)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.flush()
    await _mark(session, user.id, _regular_ids(await _episodes(session, show.id)))

    entries = await my_shows_service.list_my_shows(session, user_id=user.id, today=TODAY)

    (entry,) = entries
    assert (entry.watched_episode_count, entry.aired_episode_count) == (2, 2)


async def test_the_show_reads_finished_in_the_watched_library(session, make_user):
    """`finished` is `aired > 0 and watched >= aired and is_ended` — the same
    arithmetic, so the fraction landing on 1 is what flips the status."""
    user = await make_user()
    show = await _seed(session, show_id=964_101)
    await _mark(session, user.id, _regular_ids(await _episodes(session, show.id)))

    entries = await my_shows_service.list_watched(session, user_id=user.id, today=TODAY)

    (entry,) = entries
    assert entry.status == "finished"


async def test_watching_specials_too_does_not_exceed_one_hundred_percent(session, make_user):
    """Both halves of the fraction strip specials, or the numerator outgrows
    the denominator the moment a user ticks one."""
    user = await make_user()
    show = await _seed(session, show_id=964_102)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.flush()
    await _mark(session, user.id, [e.id for e in await _episodes(session, show.id)])

    entries = await my_shows_service.list_my_shows(session, user_id=user.id, today=TODAY)

    (entry,) = entries
    assert entry.watched_episode_count == entry.aired_episode_count == 2
    assert entry.total_episode_count == 2


# ---------------------------------------------------------------------------
# Watch Next and Upcoming
# ---------------------------------------------------------------------------


async def test_watch_next_never_offers_a_special(session, make_user):
    user = await make_user()
    show = await _seed(session, show_id=964_200)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.flush()
    await _mark(session, user.id, _regular_ids(await _episodes(session, show.id)))

    # Three unwatched aired specials remain; none of them is the next thing.
    assert await my_shows_service.list_watch_next(session, user_id=user.id, today=TODAY) == []


async def test_upcoming_reports_the_next_regular_episode_not_an_earlier_special(session, make_user):
    user = await make_user()
    show = await _seed(session, show_id=964_201, status="Returning Series")
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    session.add_all(
        [
            Episode(
                id=964_291, show_id=show.id, season_number=0, episode_number=9, air_date=FUTURE
            ),
            Episode(
                id=964_292,
                show_id=show.id,
                season_number=2,
                episode_number=1,
                air_date=FUTURE + timedelta(days=7),
            ),
        ]
    )
    await session.flush()

    entries = await my_shows_service.list_upcoming(session, user_id=user.id, today=TODAY)

    (entry,) = entries
    assert entry.episode.id == 964_292


async def test_upcoming_seasons_never_reports_the_specials_season(session, make_user):
    """Season 0 is dropped outright rather than judged on whether it has aired:
    a specials season is not a season a show is waiting for."""
    from tvbf.catalog.models import Season

    user = await make_user()
    show = await _seed(session, show_id=964_202, status="Returning Series")
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    session.add_all(
        [
            Season(id=964_281, tmdb_id=964_281, show_id=show.id, season_number=0, name="Specials"),
            Season(id=964_282, tmdb_id=964_282, show_id=show.id, season_number=2, name="Season 2"),
        ]
    )
    await session.flush()
    # Season 0's own specials have aired, but season 0 is excluded regardless —
    # so is a show whose only unaired season is the specials one.
    session.add(
        Episode(id=964_293, show_id=show.id, season_number=0, episode_number=9, air_date=FUTURE)
    )
    await session.flush()

    entries = await my_shows_service.list_upcoming_seasons(session, user_id=user.id, today=TODAY)

    assert [e.season_number for e in entries] == [2]


# ---------------------------------------------------------------------------
# Per-season progress
# ---------------------------------------------------------------------------


async def test_a_regular_season_excludes_the_copied_special_hanging_inside_it(session, make_user):
    user = await make_user()
    show = await _seed(session, show_id=964_300)
    await _mark(session, user.id, _regular_ids(await _episodes(session, show.id)))

    rows = await episode_service.list_season_progress(session, user_id=user.id, show_id=show.id)

    season_one = next(r for r in rows if r["season"] == 1)
    assert (season_one["watched"], season_one["aired"]) == (2, 2)


async def test_the_specials_season_reports_its_own_contents(session, make_user):
    """`3/9 specials watched` is useful and true — season 0's own row is not
    stripped, only a copied special inflating a *real* season."""
    user = await make_user()
    show = await _seed(session, show_id=964_301)
    specials = [e for e in await _episodes(session, show.id) if e.season_number == 0]
    await _mark(session, user.id, [specials[0].id])

    rows = await episode_service.list_season_progress(session, user_id=user.id, show_id=show.id)

    season_zero = next(r for r in rows if r["season"] == 0)
    assert (season_zero["watched"], season_zero["aired"]) == (1, 2)


# ---------------------------------------------------------------------------
# Bulk marking
# ---------------------------------------------------------------------------


async def test_marking_the_whole_show_watched_marks_no_specials(session, make_user):
    user = await make_user()
    show = await _seed(session, show_id=964_400)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.flush()

    marked = await episode_service.bulk_mark_show(session, user_id=user.id, show_id=show.id)

    assert marked == 2
    entries = await my_shows_service.list_my_shows(session, user_id=user.id, today=TODAY)
    (entry,) = entries
    # The point of skipping them: the show reads 100% rather than 2/5.
    assert entry.watched_episode_count == entry.aired_episode_count == 2


async def test_marking_the_specials_season_explicitly_still_works(session, make_user):
    """A deliberate act, and it stays available."""
    user = await make_user()
    show = await _seed(session, show_id=964_401)

    marked = await episode_service.bulk_mark_season(
        session, user_id=user.id, show_id=show.id, season_number=0
    )

    assert marked == 2
    watched = await episode_service.list_watched_episode_ids(
        session, user_id=user.id, show_id=show.id
    )
    assert sorted(watched) == sorted(
        e.id for e in await _episodes(session, show.id) if e.season_number == 0
    )


async def test_marking_a_regular_season_skips_the_copied_special_inside_it(session, make_user):
    user = await make_user()
    show = await _seed(session, show_id=964_402)

    marked = await episode_service.bulk_mark_season(
        session, user_id=user.id, show_id=show.id, season_number=1
    )

    assert marked == 2


async def test_unmarking_a_whole_show_still_removes_special_watches(session, make_user):
    """The site that must stay unfiltered: excluding anything here orphans the
    watch rows for whatever it excluded."""
    user = await make_user()
    show = await _seed(session, show_id=964_403)
    await _mark(session, user.id, [e.id for e in await _episodes(session, show.id)])

    await episode_service.bulk_unmark_show(session, user_id=user.id, show_id=show.id)

    assert (
        await episode_service.list_watched_episode_ids(session, user_id=user.id, show_id=show.id)
        == []
    )


async def test_a_watched_special_still_reads_as_watched_on_the_show_page(session, make_user):
    user = await make_user()
    show = await _seed(session, show_id=964_404)
    special_ids = _special_ids(await _episodes(session, show.id))
    await _mark(session, user.id, special_ids)

    watched = await episode_service.list_watched_episode_ids(
        session, user_id=user.id, show_id=show.id
    )

    assert sorted(watched) == sorted(special_ids)


# ---------------------------------------------------------------------------
# A show with no regular episodes
# ---------------------------------------------------------------------------


class TestSpecialsOnlyShow:
    """357 shows are 100% specials. None is tracked or watched by anyone today,
    so the rule is explicit rather than emergent: such a show has *no* progress
    — no bar, never `finished` — which is what the existing `aired > 0` guards
    already produce."""

    async def test_it_reports_zero_aired_rather_than_dividing_by_zero(self, session, make_user):
        user = await make_user()
        show = await _seed(session, show_id=964_500, regulars=0, copied_specials=0)
        session.add(UserShowWatch(user_id=user.id, show_id=show.id))
        await session.flush()

        entries = await my_shows_service.list_my_shows(session, user_id=user.id, today=TODAY)

        (entry,) = entries
        assert (entry.watched_episode_count, entry.aired_episode_count) == (0, 0)
        assert entry.total_episode_count == 0

    async def test_watching_all_its_specials_never_makes_it_finished(self, session, make_user):
        user = await make_user()
        show = await _seed(session, show_id=964_501, regulars=0, copied_specials=0)
        await _mark(session, user.id, [e.id for e in await _episodes(session, show.id)])

        # `aired > 0` fails, so the show cannot read `finished` — and with no
        # regular watches it drops out of the Watched library entirely, which
        # is the documented consequence of `list_watched` skipping watched == 0.
        assert await my_shows_service.list_watched(session, user_id=user.id, today=TODAY) == []

    async def test_bulk_marking_it_raises_not_found(self, session, make_user):
        """`bulk_mark_show` raises when a show has no aired episodes; with
        specials excluded, a specials-only show has none to mark."""
        user = await make_user()
        show = await _seed(session, show_id=964_502, regulars=0, copied_specials=0)

        with pytest.raises(NotFound):
            await episode_service.bulk_mark_show(session, user_id=user.id, show_id=show.id)
