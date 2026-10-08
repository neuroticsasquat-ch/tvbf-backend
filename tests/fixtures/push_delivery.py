"""Shared seed and stubs for the two push delivery tasks' CLI tests (NEU-1540).

`sender.send` is stubbed — the module boundary the push project spec §8 names —
so everything else in those tests is real: the candidate queries, the cap, the
delivery log's claim and retry rule, retirement, the purge, the run row and the
exit code.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime

from sqlalchemy import delete, select

from tvbf.app.models import PushDelivery, PushSubscription, UserShowWatch
from tvbf.catalog import models as m
from tvbf.config import Settings, get_settings
from tvbf.db import SessionLocal
from tvbf.push import sender

SHOW, SEASON, EPISODE = 1, 10, 100


def push_settings(**overrides) -> Settings:
    return get_settings().model_copy(
        update={
            "vapid_private_key": "private",
            "vapid_public_key": "public",
            "vapid_subject": "mailto:ops@example.com",
            **overrides,
        }
    )


def today() -> date:
    return datetime.now(UTC).date()


def airs_today_key(show_id: int = SHOW) -> str:
    return f"airs_today:{show_id}:{today().isoformat()}"


async def seed_airs_today(session) -> None:
    """One tracked-show episode airing today: Severance S2E4."""
    session.add(m.Show(id=SHOW, tmdb_id=SHOW, name="Severance", poster_path="/p.jpg"))
    await session.flush()
    session.add(m.Season(id=SEASON, tmdb_id=SEASON, show_id=SHOW, season_number=2))
    await session.flush()
    session.add(
        m.Episode(
            id=EPISODE,
            tmdb_id=EPISODE,
            show_id=SHOW,
            season_id=SEASON,
            season_number=2,
            episode_number=4,
            name="Woe’s Hollow",
            air_date=today(),
        )
    )
    await session.commit()


async def seed_another_show_airing(
    session, show_id: int = SHOW + 1, *, name: str = "Andor", status: str = "Returning Series"
) -> None:
    """Another tracked show with its own episode today — another *candidate*.
    A second episode of the first show would fold into its one push (NEU-1539)."""
    session.add(m.Show(id=show_id, tmdb_id=show_id, name=name, status=status))
    await session.flush()
    season_id = SEASON + show_id
    session.add(m.Season(id=season_id, tmdb_id=season_id, show_id=show_id, season_number=1))
    await session.flush()
    episode_id = EPISODE + show_id
    session.add(
        m.Episode(
            id=episode_id,
            tmdb_id=episode_id,
            show_id=show_id,
            season_id=season_id,
            season_number=1,
            episode_number=1,
            air_date=today(),
        )
    )
    await session.commit()


async def seed_ended_event(session, show_id: int, *, name: str = "Andor") -> str:
    """An ended show and the fresh `ended` event the delta recorded for it.
    Returns the event's notification key."""
    session.add(m.Show(id=show_id, tmdb_id=show_id, name=name, status="Ended"))
    await session.flush()
    event = m.ShowEvent(kind="ended", show_id=show_id, new_value="Ended")
    session.add(event)
    await session.commit()
    return f"ended:{event.id}"


async def subscriber(
    make_user, session, *devices: str, failure_count: int = 0, shows: tuple[int, ...] = (SHOW,)
):
    user = await make_user(email="a@example.com")
    for show_id in shows:
        session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    for device in devices or ("phone",):
        session.add(
            PushSubscription(
                user_id=user.id,
                endpoint=f"https://push.example/{device}",
                p256dh="k",
                auth="a",
                failure_count=failure_count,
            )
        )
    await session.commit()
    return user


def stub_send(
    monkeypatch,
    outcome: sender.SendOutcome | Callable[[str], sender.SendOutcome] = sender.Sent(status=201),
) -> list[tuple[str, dict]]:
    """Record every send; answer each with `outcome` (or `outcome(endpoint)`)."""
    calls: list[tuple[str, dict]] = []

    async def _send(subscription, payload, *, ttl=sender.DEFAULT_TTL_SECONDS):
        calls.append((subscription.endpoint, payload))
        assert ttl == 86400
        return outcome(subscription.endpoint) if callable(outcome) else outcome

    monkeypatch.setattr(sender, "send", _send)
    return calls


def stub_send_deleting(monkeypatch, outcome: sender.SendOutcome) -> list[str]:
    """A send during which the user deletes that very subscription."""
    calls: list[str] = []

    async def _send(subscription, payload, *, ttl=sender.DEFAULT_TTL_SECONDS):
        calls.append(subscription.endpoint)
        async with SessionLocal() as s:
            await s.execute(
                delete(PushSubscription).where(PushSubscription.endpoint == subscription.endpoint)
            )
            await s.commit()
        return outcome

    monkeypatch.setattr(sender, "send", _send)
    return calls


async def deliveries(session) -> list[PushDelivery]:
    session.expire_all()
    return list((await session.execute(select(PushDelivery).order_by(PushDelivery.id))).scalars())


async def subscriptions(session) -> list[PushSubscription]:
    session.expire_all()
    return list((await session.execute(select(PushSubscription))).scalars())


async def the_run(session) -> m.IngestRun:
    session.expire_all()
    return (await session.execute(select(m.IngestRun))).scalar_one()
