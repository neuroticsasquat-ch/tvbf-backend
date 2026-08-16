"""The one definition of "the set a user is currently seeing" (NEU-1108).

The weekly pass reads it to decide whether the payload it just compiled differs
from the one behind those recommendations; `GET /me/recommendations` reads it to
serve them. **Two implementations of that query is how the two silently
disagree** — the pass deciding a user is up to date while the surface shows
something else — so both callers come through here.

Current means *the newest `succeeded` set*, per project spec §9. The status
filter is what makes an unhappy run non-destructive rather than merely recorded:
a `failed`, `no_matches` or `insufficient_history` set is written, is newest, and
is invisible to every reader, so a provider outage at 4am on Sunday leaves last
week's recommendations standing instead of blanking the section.

One consequence is worth stating here rather than leaving it to be rediscovered
in the pass: a user whose set resolved nothing is `no_matches` (§10.1), so this
function keeps answering with whatever succeeded before it — and the regeneration
gate, reading that older hash, calls the model for them again every week even
though nothing about them changed. That is deliberate, and it is what makes a
systematic resolution break visible rather than silently permanent.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import ScalarSelect

from tvbf.app.models import (
    SET_STATUS_SUCCEEDED,
    UserRecommendation,
    UserRecommendationSet,
)
from tvbf.catalog.models import Show


def _current_set_id(user_id: UUID) -> ScalarSelect[UUID]:
    """The scalar subquery both public functions are built from.

    The ordering carries a second term on purpose. `generated_at` defaults to
    `func.now()`, which is the *transaction* timestamp, so two sets written in
    one transaction hold the identical value — and a `LIMIT 1` over a partial
    order lets Postgres return either row, which is precisely the disagreement
    between the gate and the API this module exists to prevent. The id breaks the
    tie arbitrarily but deterministically, which is all that is needed.
    """
    return (
        select(UserRecommendationSet.id)
        .where(
            UserRecommendationSet.user_id == user_id,
            UserRecommendationSet.status == SET_STATUS_SUCCEEDED,
        )
        .order_by(UserRecommendationSet.generated_at.desc(), UserRecommendationSet.id.desc())
        .limit(1)
        .scalar_subquery()
    )


async def get_current_set(session: AsyncSession, *, user_id: UUID) -> UserRecommendationSet | None:
    """The user's current set, or `None` if they have never had a succeeded one."""
    return (
        await session.execute(
            select(UserRecommendationSet).where(
                UserRecommendationSet.id == _current_set_id(user_id)
            )
        )
    ).scalar_one_or_none()


async def list_current_recommendations(
    session: AsyncSession, *, user_id: UUID
) -> list[tuple[UserRecommendation, Show]]:
    """The current set's suggestions in the model's own rank order, with their shows.

    `adult` and `deleted_upstream_at` are filtered **here**, at read time rather
    than at write time (project spec §8): a set generated in March can name a
    show tombstoned in June, and the 25-asked-for / 12-displayed headroom (§7) is
    what absorbs the loss. A write-time copy of this filter would be the weaker
    half of it and would make a resurrected show permanently unrecommendable.

    No limit is applied — how many of them a surface shows is the surface's
    decision, and §11's twelve is one such surface.
    """
    rows = (
        await session.execute(
            select(UserRecommendation, Show)
            .join(Show, Show.id == UserRecommendation.show_id)
            .where(
                UserRecommendation.set_id == _current_set_id(user_id),
                Show.adult.is_(False),
                Show.deleted_upstream_at.is_(None),
            )
            .order_by(UserRecommendation.rank)
        )
    ).all()
    return [(rec, show) for rec, show in rows]
