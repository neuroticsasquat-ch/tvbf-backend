"""What a user made of a show: LIKED, NOT LIKED or INTERESTED.

The taste signal (CONTEXT.md, project spec §5.1). **Ratings outrank completion;
completion outranks membership** — one precedence order, applied per show, and
the whole of what the model is later told about the user's history.

## The dead band overrides nothing, on purpose

A rating fires an override only at the ends: >= 3.5 stars is LIKED whatever the
completion says, <= 2.0 is NOT LIKED whatever it says, and 2.5-3.0 falls through
to behaviour untouched. Stars are `Numeric(2,1)` in half-steps 0.5-5.0
(`ck_user_show_rating_stars`) — **ten values, not five**, so the middle of the
range is two of them and not one.

3 stars means "it was fine", and completion measures fine better than a star
does: a lukewarm rating on a show somebody watched to the end would flip it out
of LIKED, inventing a verdict the user did not send. The band is therefore a
*refusal to decide*, not a gap somebody forgot to fill.

## A show rating wins outright; episode ratings only stand in for a missing one

Absent a show rating, the mean of that user's episode ratings for that show is
synthesized in its place. It is never consulted when a show rating exists, and
never averaged together with one — spec §13 puts episode ratings out of scope as
a signal of their own, and this is the single place they are read.

The mean is used raw. Rounding it to the nearest half star would move values
across a threshold (3.4 is not 3.5) to make the number look like something a user
could have typed, which is exactly what it is not.

## The 180-day clause separates abandoned from just started

NOT LIKED needs three facts, not two: episodes watched, under half of them, and
**nothing watched in 180 days**. Without the last one a show begun on Tuesday is
indistinguishable from one bailed on in 2019 — and `watched_at` genuinely spans
1994-2026 in production, so the distinction is in the data rather than a proxy for
row age.

Recency reads `ShowCompletion.last_watched_at`, which counts specials where the
progress counts do not (see `completion.py`). That asymmetry is deliberate at both
ends: watching last week's special is engagement, so it postpones abandonment;
having watched *only* specials is not progress, so it does not make a show LIKED.

## LIKED and NOT LIKED overlap, and behaviour wins the overlap

A show in My Shows, one episode in, under half, last touched two years ago
satisfies §5.1's LIKED row ("in My Shows with >= 1 episode watched") *and* its
NOT LIKED row ("... and nothing watched in 180 days") at once. The tables do not
say which fires, so the order here comes from the sentence above them:
**completion outranks membership**. An abandoned start is a behavioural verdict
and being on the list is a membership one, so NOT LIKED is tested first.

The alternative reading empties the tier. Only 8 (user, show) pairs in the
baseline are watched-but-not-added, so letting membership win confines NOT LIKED
to those eight — which is the design depending on exactly the signal §4 measured
and said not to depend on, and is why the 180-day clause exists at all. It would
also repeat, one tier up, the mistake INTERESTED was invented to avoid: calling
a show somebody bailed on in 2019 LIKED overstates the positive signal.

## Not every show gets a label

Watched, under half, and watched last week, while not in My Shows: too recent for
NOT LIKED, too little for LIKED, and INTERESTED means untouched. Nothing in §5.1
covers it, and no tier should be widened to swallow it — a show somebody started
three days ago and has not added is a fact about this week, not about taste.
`classify` answers `None` and the payload leaves it out.

## The universe is My Shows, watches, and show ratings

Membership and watches are what §5.1's tiers are written over. Show ratings join
them because a rating is the loudest thing a user can say and it outranks both —
dropping a 4.5-star show because they later removed it from My Shows would
discard exactly the signal the precedence order puts first.

Episode ratings do **not** enrol a show for the reason above: they refine a show
already in the universe and are not a signal on their own.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.repos import (
    episode_rating_repo,
    episode_watch_repo,
    show_membership_repo,
    show_rating_repo,
)
from tvbf.recommendations.completion import ShowCompletion, completion_for_shows


class TasteLabel(StrEnum):
    """The three tiers. The values are the taste payload's group keys (§5.3)."""

    LIKED = "liked"
    NOT_LIKED = "not_liked"
    INTERESTED = "interested"


LIKED_STARS = 3.5
"""At or above this, LIKED regardless of completion."""

NOT_LIKED_STARS = 2.0
"""At or below this, NOT LIKED regardless of completion. Between the two is the
dead band, which overrides nothing."""

LIKED_PCT = 50
"""Half of a show's aired episodes is enough to call it liked."""

ABANDONED_AFTER_DAYS = 180
"""Nothing watched in this long turns "started" into "given up on"."""


@dataclass(frozen=True, slots=True)
class TasteSignal:
    """One (user, show) verdict, with the facts it was reached from.

    The facts travel with the label because the payload (NEU-1104) reports two of
    them — `pct` and `stars` — and capping INTERESTED needs `added_at`. Recomputing
    them there would be a second chance to disagree with this module.
    """

    label: TasteLabel | None
    completion: ShowCompletion
    stars: float | None
    in_my_shows: bool
    added_at: datetime | None


def classify(
    *,
    completion: ShowCompletion,
    in_my_shows: bool,
    stars: float | None,
    now: datetime,
) -> TasteLabel | None:
    """The tier for one show, or `None` when no rule in §5.1 covers it.

    `stars` is the effective rating — a show rating, else the mean of the user's
    episode ratings for the show, else `None`. `now` is passed in rather than
    read, because the 180-day boundary is the one thing here a test cannot seed.
    """
    if stars is not None:
        if stars >= LIKED_STARS:
            return TasteLabel.LIKED
        if stars <= NOT_LIKED_STARS:
            return TasteLabel.NOT_LIKED

    watched = completion.watched_episodes
    # Half a show is LIKED whether or not the user ever added it. `pct` is 0
    # until something is watched, so this branch already carries "and they
    # watched some of it".
    if completion.pct >= LIKED_PCT:
        return TasteLabel.LIKED
    # Behaviour before membership: an abandoned start is NOT LIKED even when the
    # show is still in My Shows (see the docstring's note on the overlap).
    if watched >= 1 and _abandoned(completion.last_watched_at, now):
        return TasteLabel.NOT_LIKED
    if in_my_shows and watched >= 1:
        return TasteLabel.LIKED
    if in_my_shows and watched == 0:
        return TasteLabel.INTERESTED
    return None


def _abandoned(last_watched_at: datetime | None, now: datetime) -> bool:
    """Nothing watched in `ABANDONED_AFTER_DAYS`.

    A watch with no timestamp cannot be shown to be stale, so it is not treated
    as abandonment — the clause is what stops a show somebody started this week
    reading as one they gave up on, and guessing in the other direction is how
    that protection gets lost.
    """
    if last_watched_at is None:
        return False
    return now - last_watched_at >= timedelta(days=ABANDONED_AFTER_DAYS)


async def taste_for_user(
    db: AsyncSession,
    *,
    user_id: UUID,
    now: datetime | None = None,
) -> dict[int, TasteSignal]:
    """Every show this user has told us anything about, classified.

    Includes the shows that reached no tier, with `label=None` — the caller
    filters them out, rather than this function deciding that an unclassifiable
    show and one the user never touched are the same absence.
    """
    now_dt = now if now is not None else datetime.now(UTC)

    my_shows = await show_membership_repo.list_with_added_at(db, user_id)
    added_at = {show.id: added for show, added in my_shows}
    watched_ids = await episode_watch_repo.list_show_ids_with_watches(db, user_id=user_id)
    show_stars = await show_rating_repo.get_all_for_user(db, user_id=user_id)
    episode_stars = await episode_rating_repo.mean_stars_per_show_for_user(db, user_id=user_id)

    show_ids = sorted(set(added_at) | set(watched_ids) | set(show_stars))
    completions = await completion_for_shows(
        db, user_id=user_id, show_ids=show_ids, today=now_dt.date()
    )

    signals: dict[int, TasteSignal] = {}
    for show_id in show_ids:
        completion = completions[show_id]
        stars = show_stars.get(show_id, episode_stars.get(show_id))
        in_my_shows = show_id in added_at
        signals[show_id] = TasteSignal(
            label=classify(
                completion=completion,
                in_my_shows=in_my_shows,
                stars=stars,
                now=now_dt,
            ),
            completion=completion,
            stars=stars,
            in_my_shows=in_my_shows,
            added_at=added_at.get(show_id),
        )
    return signals
