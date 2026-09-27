"""What the delivery job would send today, and to whom (project spec §5.2 steps 1–3).

Selection only — nothing here sends, writes, or reads a subscription beyond
"has at least one". Kept apart from the job so every rule below is testable
without a push service: the airs-today set (Q16), the event freshness window
and its still-current check, and the per-user daily cap with its summary.

Both queries require at least one `app.push_subscription` for the user. A
candidate for a user with no device is one the job could only drop, and
counting it would spend the cap on nothing.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, exists, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import PushSubscription, User, UserEpisodeWatch, UserShowWatch
from tvbf.catalog import models as m
from tvbf.catalog.events import ShowEventKind
from tvbf.sorting import show_name_sort_key

type CandidateKind = Literal["airs_today", "summary"] | ShowEventKind

# The spec's burst guard (Q6): events older than this are never delivered, and
# nothing marks them — the window is the rule.
FRESHNESS_WINDOW_HOURS = 48
# Notifications per user per day before the rest fold into one summary.
DAILY_CAP = 5

# Which `app.user` flag opts a user into each event kind (§4.4).
_EVENT_FLAGS = {
    "premiere_set": User.notify_premiere_set,
    "premiere_moved": User.notify_premiere_moved,
    "ended": User.notify_ended,
    "revived": User.notify_revived,
}
_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Candidate:
    """One notification for one user, before it meets a subscription.

    `key` is the notification key (§5.3) — the idempotency half the delivery
    log is unique on. The fields after the ids are what `payloads.build_payload`
    renders from; each kind sets the ones its body needs and leaves the rest
    `None`. `air_date` is the **corrected** date: the episode's for airs-today,
    the season's for the two premiere kinds (what the app shows, NEU-1145).
    """

    user_id: UUID
    kind: CandidateKind
    key: str
    show_id: int | None = None
    episode_id: int | None = None
    season_id: int | None = None
    event_id: int | None = None
    show_name: str | None = None
    poster_path: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    episode_name: str | None = None
    air_date: date | None = None
    # The raw status an `ended` / `revived` event recorded (`show_event.new_value`).
    status: str | None = None
    observed_at: datetime | None = None
    # How many notifications a `summary` stands in for.
    count: int | None = None


def _has_subscription():
    return exists().where(PushSubscription.user_id == User.id)


async def airs_today_candidates(session: AsyncSession, *, today: date) -> list[Candidate]:
    """The airs-today set for every user who would receive it (Q16).

    Corrected `air_date = today`, specials (season 0) excluded, show in My
    Shows and not muted, episode not watched, account not disabled, the
    `notify_airs_today` flag set. No email-verified gate. Key
    `airs_today:{episode_id}:{air_date}`, so a date moving onto today again
    later is a new notification rather than a suppressed one.
    """
    watched = exists().where(
        UserEpisodeWatch.user_id == User.id, UserEpisodeWatch.episode_id == m.Episode.id
    )
    rows = await session.execute(
        select(
            User.id,
            m.Show.id,
            m.Show.name,
            m.Show.poster_path,
            m.Episode.id,
            m.Episode.season_number,
            m.Episode.episode_number,
            m.Episode.name,
        )
        .join(UserShowWatch, UserShowWatch.user_id == User.id)
        .join(m.Episode, m.Episode.show_id == UserShowWatch.show_id)
        .join(m.Show, m.Show.id == m.Episode.show_id)
        .where(
            User.notify_airs_today.is_(True),
            User.disabled_at.is_(None),
            _has_subscription(),
            UserShowWatch.muted.is_(false()),
            m.Episode.air_date == today,
            m.Episode.season_number > 0,
            ~watched,
        )
        .order_by(User.id, m.Episode.id)
    )
    return [
        Candidate(
            user_id=user_id,
            kind="airs_today",
            key=f"airs_today:{episode_id}:{today.isoformat()}",
            show_id=show_id,
            episode_id=episode_id,
            show_name=show_name,
            poster_path=poster_path,
            season_number=season_number,
            episode_number=episode_number,
            episode_name=episode_name,
            air_date=today,
        )
        for (
            user_id,
            show_id,
            show_name,
            poster_path,
            episode_id,
            season_number,
            episode_number,
            episode_name,
        ) in rows
    ]


def is_still_current(
    kind: ShowEventKind,
    *,
    new_value: str | None,
    raw_air_date: date | None,
    air_date: date | None,
    is_ended: bool,
    today: date,
) -> bool:
    """Whether the fact a catalog event recorded is still true (§5.2 step 2).

    For the premiere kinds the comparison is against the season's **raw** date,
    `coalesce(tmdb_air_date, air_date)` — the value detection compared and
    stored in `new_value`. The corrected `air_date` differs from it by the
    offset on every offset-corrected show and would never match. The corrected
    date is what must still be today or later: a premiere that has since aired
    is no longer news.
    """
    match kind:
        case "premiere_set" | "premiere_moved":
            return (
                raw_air_date is not None
                and raw_air_date.isoformat() == new_value
                and air_date is not None
                and air_date >= today
            )
        case "ended":
            return is_ended
        case "revived":
            return not is_ended


async def event_candidates(
    session: AsyncSession, *, now: datetime, window_hours: int = FRESHNESS_WINDOW_HOURS
) -> list[Candidate]:
    """Fresh, still-current catalog events, one candidate per interested user.

    Interested: tracks the show, has not muted it, has the kind's `notify_*`
    flag, is not disabled. The SQL does the joining and the window; the
    still-current check runs in Python over the season and show as they stand,
    because it is one rule per kind over values the query already returns. Key
    `{kind}:{event_id}`.
    """
    today = now.astimezone(UTC).date()
    kind_flag = or_(*(and_(m.ShowEvent.kind == kind, flag) for kind, flag in _EVENT_FLAGS.items()))
    rows = await session.execute(
        select(
            User.id,
            m.ShowEvent.id,
            m.ShowEvent.kind,
            m.ShowEvent.season_id,
            m.ShowEvent.new_value,
            m.ShowEvent.observed_at,
            m.Show.id,
            m.Show.name,
            m.Show.poster_path,
            m.Show.is_ended,
            m.Season.season_number,
            m.Season.air_date,
            m.Season.tmdb_air_date,
        )
        .select_from(m.ShowEvent)
        .join(m.Show, m.Show.id == m.ShowEvent.show_id)
        .outerjoin(m.Season, m.Season.id == m.ShowEvent.season_id)
        .join(UserShowWatch, UserShowWatch.show_id == m.ShowEvent.show_id)
        .join(User, User.id == UserShowWatch.user_id)
        .where(
            m.ShowEvent.observed_at >= now - timedelta(hours=window_hours),
            UserShowWatch.muted.is_(false()),
            User.disabled_at.is_(None),
            _has_subscription(),
            kind_flag,
        )
        .order_by(m.ShowEvent.observed_at, m.ShowEvent.id, User.id)
    )
    candidates: list[Candidate] = []
    for (
        user_id,
        event_id,
        kind,
        season_id,
        new_value,
        observed_at,
        show_id,
        show_name,
        poster_path,
        is_ended,
        season_number,
        air_date,
        tmdb_air_date,
    ) in rows:
        if not is_still_current(
            kind,
            new_value=new_value,
            raw_air_date=tmdb_air_date or air_date,
            air_date=air_date,
            is_ended=is_ended,
            today=today,
        ):
            continue
        candidates.append(
            Candidate(
                user_id=user_id,
                kind=kind,
                key=f"{kind}:{event_id}",
                show_id=show_id,
                season_id=season_id,
                event_id=event_id,
                show_name=show_name,
                poster_path=poster_path,
                season_number=season_number,
                air_date=air_date,
                status=new_value if kind in ("ended", "revived") else None,
                observed_at=observed_at,
            )
        )
    return candidates


def group_by_user(candidates: Iterable[Candidate]) -> dict[UUID, list[Candidate]]:
    """Candidates keyed by the user they are for, input order kept."""
    grouped: dict[UUID, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.user_id].append(candidate)
    return dict(grouped)


def apply_cap(
    candidates_by_user: Mapping[UUID, Sequence[Candidate]], cap: int = DAILY_CAP, *, today: date
) -> dict[UUID, list[Candidate]]:
    """Each user's notifications in delivery order, capped (§5.2 step 3).

    Airs-today first, by show name (article-stripped, as My Shows sorts), then
    events oldest first. Past `cap`, the remainder is replaced by one `summary`
    candidate keyed `summary:{user_id}:{today}` — so a user can receive
    `cap + 1` pushes, the last one standing in for the rest.
    """
    capped: dict[UUID, list[Candidate]] = {}
    for user_id, candidates in candidates_by_user.items():
        airs = sorted(
            (c for c in candidates if c.kind == "airs_today"),
            key=lambda c: (
                show_name_sort_key(c.show_name or ""),
                c.season_number or 0,
                c.episode_number or 0,
            ),
        )
        events = sorted(
            (c for c in candidates if c.kind != "airs_today"),
            key=lambda c: (c.observed_at or _EPOCH, c.event_id or 0),
        )
        ordered = airs + events
        kept = ordered[:cap]
        overflow = len(ordered) - len(kept)
        if overflow:
            kept.append(
                Candidate(
                    user_id=user_id,
                    kind="summary",
                    key=f"summary:{user_id}:{today.isoformat()}",
                    count=overflow,
                )
            )
        capped[user_id] = kept
    return capped
