"""Specials sort last — at the season grain and inside the episode list.

TMDB models a special as season 0 with a real episode number, and 0 sorts ahead
of 1, so 12,151 shows listed their specials *first*. NEU-1062 moves the whole
season to the end without disturbing anything inside it — in particular the
copied-special rule `catalog/episodes.py` already owned, which still trails its
own real season rather than joining season 0.
"""

from datetime import date

from tvbf.app.repos import episode_repo
from tvbf.catalog import models as m
from tvbf.catalog.browse_queries import get_show_episodes, get_show_seasons

SHOW_ID = 963_100


async def _seed(session) -> m.Show:
    """Seasons 0, 1, 2 — season 1 also carrying a copied special."""
    show = m.Show(id=SHOW_ID, tmdb_id=SHOW_ID, name="Ordered")
    session.add(show)
    await session.flush()
    session.add_all(
        [
            m.Season(id=1, tmdb_id=1, show_id=SHOW_ID, season_number=0, name="Specials"),
            m.Season(id=2, tmdb_id=2, show_id=SHOW_ID, season_number=1, name="Season 1"),
            m.Season(id=3, tmdb_id=3, show_id=SHOW_ID, season_number=2, name="Season 2"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            m.Episode(id=SHOW_ID + 1, show_id=SHOW_ID, season_number=0, episode_number=1),
            m.Episode(id=SHOW_ID + 2, show_id=SHOW_ID, season_number=1, episode_number=1),
            m.Episode(id=SHOW_ID + 3, show_id=SHOW_ID, season_number=1, episode_number=2),
            m.Episode(id=SHOW_ID + 4, show_id=SHOW_ID, season_number=1, episode_number=-1),
            m.Episode(id=SHOW_ID + 5, show_id=SHOW_ID, season_number=2, episode_number=1),
        ]
    )
    await session.flush()
    return show


async def test_the_specials_season_is_listed_last(session):
    await _seed(session)

    seasons = await get_show_seasons(session, SHOW_ID)

    assert [s.season_number for s in seasons] == [1, 2, 0]


async def test_specials_sort_last_in_the_episode_list_and_copied_ones_stay_put(session):
    await _seed(session)

    episodes = await get_show_episodes(session, SHOW_ID, season=None)

    # Season 1's copied special still trails season 1 — it does not migrate to
    # the end of the show — and season 0 comes after everything.
    assert [(e.season_number, e.episode_number) for e in episodes] == [
        (1, 1),
        (1, 2),
        (1, -1),
        (2, 1),
        (0, 1),
    ]


async def test_a_show_with_no_specials_is_ordered_exactly_as_before(session):
    show = m.Show(id=963_200, tmdb_id=963_200, name="No specials")
    session.add(show)
    await session.flush()
    session.add_all(
        [
            m.Episode(id=963_201, show_id=show.id, season_number=2, episode_number=1),
            m.Episode(id=963_202, show_id=show.id, season_number=1, episode_number=2),
            m.Episode(id=963_203, show_id=show.id, season_number=1, episode_number=1),
        ]
    )
    await session.flush()

    episodes = await get_show_episodes(session, show.id, season=None)

    assert [(e.season_number, e.episode_number) for e in episodes] == [(1, 1), (1, 2), (2, 1)]


async def test_the_specials_season_is_never_an_upcoming_season(session, make_user):
    """A season 0 whose specials have not aired is not what a show is waiting
    for — otherwise every show TMDB has announced a special for reads as having
    a season on the way."""
    from tvbf.app.models import UserShowWatch
    from tvbf.app.repos import season_repo

    user = await make_user()
    await _seed(session)
    session.add(UserShowWatch(user_id=user.id, show_id=SHOW_ID))
    # Nothing has aired at all, so seasons 1 and 2 are legitimately unaired.
    await session.flush()

    unaired = await season_repo.unaired_for_shows(session, [SHOW_ID], date(2026, 8, 12))

    assert [s.season_number for s in unaired] == [1, 2]


async def test_next_unwatched_walks_regular_seasons_in_order(session, make_user):
    """The ordering change must not reshuffle what Watch Next offers: with
    nothing watched, the first regular episode is still season 1 episode 1."""
    user = await make_user()
    await _seed(session)

    nxt = await episode_repo.next_unwatched(session, user_id=user.id, show_id=SHOW_ID)

    assert nxt is not None
    assert (nxt.season_number, nxt.episode_number) == (1, 1)
