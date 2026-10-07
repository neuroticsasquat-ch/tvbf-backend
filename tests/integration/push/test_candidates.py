from datetime import UTC, date, datetime, timedelta

from sqlalchemy import update

from tvbf.app.models import PushSubscription, User, UserEpisodeWatch, UserShowWatch
from tvbf.catalog import models as m
from tvbf.push.candidates import AiredEpisode, airs_today_candidates, event_candidates

TODAY = date(2026, 9, 26)
NOW = datetime(2026, 9, 26, 13, 0, tzinfo=UTC)
SHOW, SEASON, EPISODE = 1, 10, 100


async def _catalog(session, *, status: str | None = "Returning Series") -> None:
    session.add(
        m.Show(id=SHOW, tmdb_id=SHOW, name="Severance", poster_path="/p.jpg", status=status)
    )
    await session.flush()
    session.add(
        m.Season(
            id=SEASON,
            tmdb_id=SEASON,
            show_id=SHOW,
            season_number=2,
            air_date=date(2026, 10, 5),
            tmdb_air_date=date(2026, 10, 4),
        )
    )
    await session.flush()


async def _episode(
    session,
    *,
    episode_id=EPISODE,
    show_id=SHOW,
    season_id=SEASON,
    season_number=2,
    episode_number=4,
    name: str | None = "Woe’s Hollow",
    air_date=TODAY,
) -> None:
    session.add(
        m.Episode(
            id=episode_id,
            tmdb_id=episode_id,
            show_id=show_id,
            season_id=season_id,
            season_number=season_number,
            episode_number=episode_number,
            name=name,
            air_date=air_date,
        )
    )
    await session.flush()


async def _subscriber(make_user, session, email="a@example.com", *, tracks=True, muted=False):
    user = await make_user(email=email)
    session.add(
        PushSubscription(
            user_id=user.id, endpoint=f"https://push.example/{email}", p256dh="k", auth="a"
        )
    )
    if tracks:
        session.add(UserShowWatch(user_id=user.id, show_id=SHOW, muted=muted))
    await session.commit()
    return user


async def _set(session, user, **values) -> None:
    await session.execute(update(User).where(User.id == user.id).values(**values))
    await session.commit()


# --- airs today (Q16) ---------------------------------------------------------


async def test_an_unwatched_episode_airing_today_is_a_candidate(session, make_user):
    await _catalog(session)
    await _episode(session)
    user = await _subscriber(make_user, session)

    [candidate] = await airs_today_candidates(session, today=TODAY)

    assert candidate.user_id == user.id
    assert candidate.kind == "airs_today"
    assert candidate.key == f"airs_today:{SHOW}:2026-09-26"
    assert (candidate.show_id, candidate.episode_id) == (SHOW, EPISODE)
    assert (candidate.season_number, candidate.episode_number) == (2, 4)
    assert (candidate.show_name, candidate.episode_name) == ("Severance", "Woe’s Hollow")
    assert candidate.poster_path == "/p.jpg"
    assert candidate.episodes == (AiredEpisode(EPISODE, 2, 4, "Woe’s Hollow"),)


# --- one candidate per show (Q8, NEU-1539) ------------------------------------


async def test_every_episode_of_a_show_airing_today_folds_into_one_candidate(session, make_user):
    await _catalog(session)
    # Inserted out of order, and with a season-1 straggler, to pin the sort.
    await _episode(session, episode_id=EPISODE + 2, episode_number=6, name=None)
    await _episode(session, episode_id=EPISODE, episode_number=5)
    await _episode(session, episode_id=EPISODE + 1, season_number=1, episode_number=9, name="Nine")
    await _subscriber(make_user, session)

    [candidate] = await airs_today_candidates(session, today=TODAY)

    assert candidate.key == f"airs_today:{SHOW}:2026-09-26"
    assert candidate.episodes == (
        AiredEpisode(EPISODE + 1, 1, 9, "Nine"),
        AiredEpisode(EPISODE, 2, 5, "Woe’s Hollow"),
        AiredEpisode(EPISODE + 2, 2, 6, None),
    )
    # The single fields are the first episode's.
    assert (candidate.episode_id, candidate.season_number, candidate.episode_number) == (
        EPISODE + 1,
        1,
        9,
    )
    assert candidate.episode_name == "Nine"


async def test_a_watched_episode_leaves_the_rest_of_the_drop(session, make_user):
    await _catalog(session)
    await _episode(session, episode_id=EPISODE, episode_number=1)
    await _episode(session, episode_id=EPISODE + 1, episode_number=2)
    await _episode(session, episode_id=EPISODE + 2, episode_number=3)
    user = await _subscriber(make_user, session)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=EPISODE + 1))
    await session.commit()

    [candidate] = await airs_today_candidates(session, today=TODAY)

    assert [e.episode_number for e in candidate.episodes] == [1, 3]


async def test_each_show_airing_today_is_its_own_candidate(session, make_user):
    await _catalog(session)
    await _episode(session)
    other_show, other_season = SHOW + 1, SEASON + 1
    session.add(m.Show(id=other_show, tmdb_id=other_show, name="Andor", status="Returning Series"))
    await session.flush()
    session.add(
        m.Season(id=other_season, tmdb_id=other_season, show_id=other_show, season_number=1)
    )
    await session.flush()
    await _episode(
        session,
        episode_id=EPISODE + 1,
        show_id=other_show,
        season_id=other_season,
        season_number=1,
        episode_number=1,
    )
    user = await _subscriber(make_user, session)
    session.add(UserShowWatch(user_id=user.id, show_id=other_show))
    await session.commit()

    found = await airs_today_candidates(session, today=TODAY)

    assert [(c.show_id, c.key, len(c.episodes)) for c in found] == [
        (SHOW, f"airs_today:{SHOW}:2026-09-26", 1),
        (other_show, f"airs_today:{other_show}:2026-09-26", 1),
    ]


async def test_airs_today_reads_the_corrected_air_date(session, make_user):
    await _catalog(session)
    await _episode(session, air_date=TODAY + timedelta(days=1))
    await _subscriber(make_user, session)

    assert await airs_today_candidates(session, today=TODAY) == []


async def test_specials_never_air_today(session, make_user):
    await _catalog(session)
    await _episode(session, season_number=0)
    await _subscriber(make_user, session)

    assert await airs_today_candidates(session, today=TODAY) == []


async def test_a_show_not_in_my_shows_is_not_a_candidate(session, make_user):
    await _catalog(session)
    await _episode(session)
    await _subscriber(make_user, session, tracks=False)

    assert await airs_today_candidates(session, today=TODAY) == []


async def test_a_muted_show_is_not_a_candidate(session, make_user):
    await _catalog(session)
    await _episode(session)
    await _subscriber(make_user, session, muted=True)

    assert await airs_today_candidates(session, today=TODAY) == []


async def test_a_watched_episode_is_not_a_candidate(session, make_user):
    await _catalog(session)
    await _episode(session)
    user = await _subscriber(make_user, session)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=EPISODE))
    await session.commit()

    assert await airs_today_candidates(session, today=TODAY) == []


async def test_a_disabled_account_gets_nothing(session, make_user):
    await _catalog(session)
    await _episode(session)
    user = await _subscriber(make_user, session)
    await _set(session, user, disabled_at=NOW)

    assert await airs_today_candidates(session, today=TODAY) == []


async def test_the_airs_today_flag_opts_out(session, make_user):
    await _catalog(session)
    await _episode(session)
    user = await _subscriber(make_user, session)
    await _set(session, user, notify_airs_today=False)

    assert await airs_today_candidates(session, today=TODAY) == []


async def test_a_user_with_no_subscription_is_skipped(session, make_user):
    await _catalog(session)
    await _episode(session)
    user = await make_user()
    session.add(UserShowWatch(user_id=user.id, show_id=SHOW))
    await session.commit()

    assert await airs_today_candidates(session, today=TODAY) == []


async def test_an_unverified_email_is_no_gate(session, make_user):
    await _catalog(session)
    await _episode(session)
    user = await _subscriber(make_user, session)

    assert user.email_verified_at is None
    assert len(await airs_today_candidates(session, today=TODAY)) == 1


async def test_each_tracking_user_gets_their_own_candidate(session, make_user):
    await _catalog(session)
    await _episode(session)
    alice = await _subscriber(make_user, session, "alice@example.com")
    bob = await _subscriber(make_user, session, "bob@example.com")

    found = await airs_today_candidates(session, today=TODAY)

    assert {c.user_id for c in found} == {alice.id, bob.id}
    assert {c.key for c in found} == {f"airs_today:{SHOW}:2026-09-26"}


# --- events -------------------------------------------------------------------


async def _event(session, kind, *, new_value, season_id=None, observed_at=NOW) -> int:
    event = m.ShowEvent(kind=kind, show_id=SHOW, season_id=season_id, new_value=new_value)
    session.add(event)
    await session.flush()
    await session.execute(
        update(m.ShowEvent).where(m.ShowEvent.id == event.id).values(observed_at=observed_at)
    )
    await session.commit()
    return event.id


async def test_a_fresh_current_premiere_is_a_candidate(session, make_user):
    await _catalog(session)
    user = await _subscriber(make_user, session)
    # The raw date detection stored, not the corrected 10-05.
    event_id = await _event(session, "premiere_set", new_value="2026-10-04", season_id=SEASON)

    [candidate] = await event_candidates(session, now=NOW)

    assert candidate.user_id == user.id
    assert candidate.kind == "premiere_set"
    assert candidate.key == f"premiere_set:{event_id}"
    assert (candidate.show_id, candidate.season_id, candidate.event_id) == (SHOW, SEASON, event_id)
    assert candidate.season_number == 2
    # The corrected date is what the body renders.
    assert candidate.air_date == date(2026, 10, 5)
    assert candidate.observed_at == NOW


async def test_a_premiere_moved_again_since_is_dropped(session, make_user):
    await _catalog(session)
    await _subscriber(make_user, session)
    await _event(session, "premiere_moved", new_value="2026-09-30", season_id=SEASON)

    assert await event_candidates(session, now=NOW) == []


async def test_a_premiere_that_has_since_aired_is_dropped(session, make_user):
    await _catalog(session)
    await _subscriber(make_user, session)
    await session.execute(
        update(m.Season)
        .where(m.Season.id == SEASON)
        .values(air_date=TODAY - timedelta(days=1), tmdb_air_date=None)
    )
    await _event(
        session,
        "premiere_moved",
        new_value=(TODAY - timedelta(days=1)).isoformat(),
        season_id=SEASON,
    )

    assert await event_candidates(session, now=NOW) == []


async def test_ended_is_delivered_while_the_show_is_ended(session, make_user):
    await _catalog(session, status="Canceled")
    await _subscriber(make_user, session)
    await _event(session, "ended", new_value="Canceled")

    [candidate] = await event_candidates(session, now=NOW)

    assert (candidate.kind, candidate.status, candidate.season_id) == ("ended", "Canceled", None)


async def test_ended_is_dropped_once_the_show_is_airing_again(session, make_user):
    await _catalog(session, status="Returning Series")
    await _subscriber(make_user, session)
    await _event(session, "ended", new_value="Ended")

    assert await event_candidates(session, now=NOW) == []


async def test_revived_is_delivered_while_the_show_is_not_ended(session, make_user):
    await _catalog(session, status="Returning Series")
    await _subscriber(make_user, session)
    await _event(session, "revived", new_value="Returning Series")

    assert [c.kind for c in await event_candidates(session, now=NOW)] == ["revived"]


async def test_revived_is_dropped_once_the_show_ended_again(session, make_user):
    await _catalog(session, status="Ended")
    await _subscriber(make_user, session)
    await _event(session, "revived", new_value="Returning Series")

    assert await event_candidates(session, now=NOW) == []


async def test_events_outside_the_window_are_never_delivered(session, make_user):
    await _catalog(session, status="Ended")
    await _subscriber(make_user, session)
    edge = NOW - timedelta(hours=48)
    on_edge = await _event(session, "ended", new_value="Ended", observed_at=edge)
    await _event(session, "ended", new_value="Ended", observed_at=edge - timedelta(seconds=1))

    assert [c.event_id for c in await event_candidates(session, now=NOW)] == [on_edge]


async def test_the_window_is_a_parameter(session, make_user):
    await _catalog(session, status="Ended")
    await _subscriber(make_user, session)
    await _event(session, "ended", new_value="Ended", observed_at=NOW - timedelta(hours=3))

    assert await event_candidates(session, now=NOW, window_hours=2) == []


async def test_event_recipients_must_track_unmuted_and_be_enabled(session, make_user):
    await _catalog(session, status="Ended")
    keeps = await _subscriber(make_user, session, "keeps@example.com")
    await _subscriber(make_user, session, "untracked@example.com", tracks=False)
    await _subscriber(make_user, session, "muted@example.com", muted=True)
    disabled = await _subscriber(make_user, session, "disabled@example.com")
    await _set(session, disabled, disabled_at=NOW)
    await _event(session, "ended", new_value="Ended")

    assert [c.user_id for c in await event_candidates(session, now=NOW)] == [keeps.id]


async def test_each_kind_has_its_own_flag(session, make_user):
    await _catalog(session, status="Ended")
    user = await _subscriber(make_user, session)
    await _event(session, "premiere_set", new_value="2026-10-04", season_id=SEASON)
    await _event(session, "premiere_moved", new_value="2026-10-04", season_id=SEASON)
    await _event(session, "ended", new_value="Ended")

    await _set(session, user, notify_premiere_set=False, notify_ended=False)

    assert [c.kind for c in await event_candidates(session, now=NOW)] == ["premiere_moved"]

    await _set(session, user, notify_premiere_moved=False, notify_ended=True)

    assert [c.kind for c in await event_candidates(session, now=NOW)] == ["ended"]


async def test_the_revived_flag_opts_out(session, make_user):
    await _catalog(session)
    user = await _subscriber(make_user, session)
    await _event(session, "revived", new_value="Returning Series")
    await _set(session, user, notify_revived=False)

    assert await event_candidates(session, now=NOW) == []
