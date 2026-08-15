"""How far through a show a user is, measured against **aired** episodes.

The numerator of the taste signal's middle tier (project spec §5.1) and the whole
of §5.2. Three decisions are recorded here because each of them is invisible in
the arithmetic and wrong in a way nothing downstream would catch.

## The denominator is aired episodes, not total

A currently-airing show a user is fully caught up on has episodes that do not
exist yet. Against the total those count against them: the show reads ~85% and
**can never reach 100%**, so every ongoing series lands below the 50% tier
boundary's confident end and "caught up" is indistinguishable from "gave up two
thirds of the way in". Caught up and finished are the same fact about taste, so
both have to land at 100%.

The cost of the choice is that a percentage is a statement about *today* — a show
at 100% drops back below it the week its next episode airs, which is correct and
is why nothing caches it.

## 0 and 100 are reserved for the endpoints

`completion_pct` never rounds *into* either: a user one episode short of a
500-episode run is 99, and a user one episode into it is 1. Otherwise "finished"
and "never started" — the two values the tier rules actually lean on — would both
be claims the data does not support.

Only the lower end needs a term in the code. Flooring already reserves 100 (below
100% of a run, `100 * w // a` cannot reach it), and flooring is the right rule in
its own right: a percentage should not overstate how far through somebody is, so
`pct >= 50` means at least half. The `max(1, ...)` is the one place that rule is
knowingly broken, in the direction that keeps 0 meaning nothing at all.

## A show with nothing aired is 100 once anything is watched

`aired_episodes` counts only episodes with an air date on or before today, so a
show whose episodes are entirely undated has a denominator of zero and no
percentage is computable. Both available answers are inventions; 100 is the safer
one. The user *did* watch it, and §5.1's LIKED tier already reads "in My Shows
with >= 1 episode watched" — reporting 0 instead would leave a show somebody
finished one 180-day gap away from NOT LIKED.

## Specials count in neither half, but any watch counts as recency

Both counts come from the pair `episode_repo.count_aired_per_show` /
`episode_watch_repo.count_watched_per_show`, which strip specials from numerator
and denominator alike (`catalog/episodes.py`, and the ledger test that pins it) —
a user who watches every regular episode reads 100% whether they watched the
specials or not.

`last_watched_at` deliberately does not strip them. It answers "when did this
person last engage with this show", which the 180-day abandonment clause reads,
and watching a special last week is engagement. It is `latest_watched_per_show`,
already `EXCLUDE_NOTHING` in the ledger for that reason.

The asymmetry is visible in the output and consumers have to expect it: somebody
who has watched *only* specials reads `watched_episodes = 0` beside a non-null
`last_watched_at`. That is the right pair of facts — no regular episode moved,
but they were here last week — and it is why "zero episodes watched" and "never
touched this show" are not the same test.

## Why this composes repo functions instead of running one query

One grouped query would save two round trips on a weekly per-user job, and would
buy them by restating three specials predicates that are pinned by
`tests/integration/app/repos/test_specials_ledger.py` in a module that ledger
does not read. The predicates are the part that is easy to get wrong; the round
trips are not the part that is expensive.
"""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.repos import episode_repo, episode_watch_repo


def completion_pct(*, watched_episodes: int, aired_episodes: int) -> int:
    """Percent of the show's aired episodes the user has watched, 0-100.

    Rounds down, and reserves both endpoints (see the module docstring).

    `watched_episodes` can exceed `aired_episodes`: marking an unaired episode
    watched is permitted, and an episode with no air date at all is watchable but
    uncounted in the denominator. Both saturate at 100 rather than overflowing
    it — which is also what answers the zero denominator, so the order of these
    two branches is the whole of that decision.
    """
    if watched_episodes <= 0:
        return 0
    if watched_episodes >= aired_episodes:
        return 100
    return max(1, (100 * watched_episodes) // aired_episodes)


@dataclass(frozen=True, slots=True)
class ShowCompletion:
    """One (user, show) pair's progress, and when they last touched it."""

    watched_episodes: int
    aired_episodes: int
    last_watched_at: datetime | None

    @property
    def pct(self) -> int:
        """The derived percentage — a property so it cannot drift from the counts."""
        return completion_pct(
            watched_episodes=self.watched_episodes,
            aired_episodes=self.aired_episodes,
        )


async def completion_for_shows(
    db: AsyncSession,
    *,
    user_id: UUID,
    show_ids: list[int],
    today: date | None = None,
) -> dict[int, ShowCompletion]:
    """Completion for each of `show_ids`, keyed by show id.

    Every requested show gets an entry, including one the user has watched
    nothing of — a My Shows row with zero watches is the INTERESTED tier, so an
    absent key would make the caller decide what a missing show means and every
    caller would have to decide the same way.
    """
    if not show_ids:
        return {}

    today_d = today if today is not None else date.today()
    watched_counts = await episode_watch_repo.count_watched_per_show(
        db, user_id=user_id, show_ids=show_ids
    )
    aired_counts = await episode_repo.count_aired_per_show(db, show_ids, today_d)
    last_watched = await episode_watch_repo.latest_watched_per_show(
        db, user_id=user_id, show_ids=show_ids
    )

    return {
        show_id: ShowCompletion(
            watched_episodes=watched_counts.get(show_id, 0),
            aired_episodes=aired_counts.get(show_id, 0),
            last_watched_at=last_watched.get(show_id),
        )
        for show_id in show_ids
    }
