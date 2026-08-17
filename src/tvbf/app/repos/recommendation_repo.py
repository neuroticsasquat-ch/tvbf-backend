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

`write_set` (NEU-1109) is the other half and lives here for the same reason the
two readers do: it spans both tables, and "a set plus its rows, written
together" is the property that makes the weekly swap atomic. A caller assembling
the two by hand is a caller that can write a set with no rows and commit it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
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
from tvbf.recommendations import exclusion


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
    """The current set's suggestions this reader has not already met, in rank order.

    `adult` and `deleted_upstream_at` are filtered **here**, at read time rather
    than at write time (project spec §8): a set generated in March can name a
    show tombstoned in June, and the 25-asked-for / 12-displayed headroom (§7) is
    what absorbs the loss. A write-time copy of this filter would be the weaker
    half of it and would make a resurrected show permanently unrecommendable.

    A show the reader has a record for is suppressed on the same terms and for
    the same reason (NEU-1175). §8's never-recommend rule was enforced only at
    generation time, so a show added to My Shows on Monday held a card until
    Sunday's pass superseded the whole set. It belongs *here* rather than in the
    service because what a reader's current set **is** already includes what they
    have not already met — put it a layer up and the weekly pass and the API can
    come to disagree about it, which is the thing this module exists to prevent.
    The rule itself is `recommendations/exclusion.py`'s, expressed once and used
    at both ends. The weekly pass is untouched by it: the pass reads
    `get_current_set` for the hash, and this function's only `src/` caller is the
    API service.

    The suppression is a live join, never a stored flag: removing the record
    brings the suggestion back, and **the set's rows are never mutated or
    deleted** — immutability is what makes the weekly swap atomic (§9) and what
    keeps `raw_response` usable as the record of what the model actually said.
    `rank` is likewise never renumbered; it is the model's own ordering, so the
    values stay non-contiguous exactly as the `adult` filter already leaves them.

    No limit is applied — how many of them a surface shows is the surface's
    decision, and §11's twelve is one such surface. The slice being taken off the
    front of this order is what promotes the next suggestion for free, so fewer
    than twelve is a normal answer rather than something to backfill.
    """
    rows = (
        await session.execute(
            select(UserRecommendation, Show)
            .join(Show, Show.id == UserRecommendation.show_id)
            .where(
                UserRecommendation.set_id == _current_set_id(user_id),
                Show.adult.is_(False),
                Show.deleted_upstream_at.is_(None),
                UserRecommendation.show_id.not_in(exclusion.show_ids_with_a_record(user_id)),
            )
            .order_by(UserRecommendation.rank)
        )
    ).all()
    return [(rec, show) for rec, show in rows]


@dataclass(frozen=True, slots=True)
class NewRecommendation:
    """One resolved suggestion on its way into a set, minus its rank.

    Rank is the model's own ordering and is assigned by `write_set` from the
    order of the sequence it is given, so that "the model's order is the
    ordering" is a property of one function rather than a convention every
    caller has to hold up. `uq_user_recommendation_set_rank` is what would
    catch a caller getting it wrong, and catching it at write time in
    production is not catching it.
    """

    show_id: int
    reason: str
    matched_via: str
    recovered_from: str | None = None
    """The raw dressed title this row was recovered from, on the rare row that
    was (NEU-1173). Defaulted because it is NULL on every ordinary row and a
    caller that has never heard of the fallback should not have to say so."""


async def write_set(
    session: AsyncSession,
    *,
    user_id: UUID,
    status: str,
    payload_hash: str,
    prompt_version: str,
    model: str,
    compiled_payload: dict,
    raw_response: dict | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    recommendations: Sequence[NewRecommendation] = (),
) -> UserRecommendationSet:
    """Record one attempt for one user, with whatever it resolved.

    The set and its rows are added together and flushed once, so a caller
    committing after this gets both or neither — which is what makes the weekly
    swap atomic (project spec §9). Nothing is deleted first: the previous set
    simply stops being the newest, so a failure here leaves last week's
    recommendations standing.

    Every status comes through here, including the three no reader will ever
    see (`failed`, `no_matches`, `insufficient_history`). They carry a
    `payload_hash` like a succeeded one because all four are reached *after*
    the payload is compiled, and the row is the only place a failure becomes
    visible at 3-5 users.

    The caller commits, on `user_repo.create`'s terms.
    """
    recommendation_set = UserRecommendationSet(
        user_id=user_id,
        status=status,
        payload_hash=payload_hash,
        prompt_version=prompt_version,
        model=model,
        compiled_payload=compiled_payload,
        raw_response=raw_response,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    session.add(recommendation_set)
    # Flushed before the rows because they need the generated set id, and
    # `models.py` declares no `relationship()` anywhere — SQLAlchemy's unit of
    # work does not infer FK-based insert order without one.
    await session.flush()
    session.add_all(
        [
            UserRecommendation(
                set_id=recommendation_set.id,
                rank=rank,
                show_id=row.show_id,
                reason=row.reason,
                matched_via=row.matched_via,
                recovered_from=row.recovered_from,
            )
            for rank, row in enumerate(recommendations, start=1)
        ]
    )
    await session.flush()
    return recommendation_set
