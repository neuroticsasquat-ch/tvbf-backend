"""The never-recommend rule: every show a user already has a record for (§8).

Project spec §8 says a show the user has a record for is never recommended, and
names its four sources — My Shows membership, a show rating, any episode watch,
any episode rating. Until NEU-1175 that sentence was enforced once, at
generation time, in Python inside `payload.build_payload`; a show the user acted
on afterwards kept its card until Sunday's pass superseded the whole set.

It is now enforced at **both** ends — at write time by the payload builder, and
at read time by `recommendation_repo.list_current_recommendations` — which is
exactly why the rule is expressed here and only here. Two expressions of one
sentence, one in Python and one in SQL, is the failure mode
`recommendation_repo`'s own docstring exists to prevent, one layer up. **A fifth
source belongs in `show_ids_with_a_record` and nowhere else**, and adding it
there changes both ends at once.

The two functions are one answer in two shapes: a `Select` for the read path,
which wants it as an `IN` operand inside a single statement, and a materialised
`frozenset` for the payload builder, which wants it in Python. They are built
from the same branches so they cannot answer differently.

This module imports **models only**. `recommendations/` already imports
`app.repos` (`taste`, `payload`, `completion`), so a repo importing this module
makes the package edge two-way — acyclic at module level, and only because
nothing here imports a repo. Keep it that way. The precedent for a rule module
that repos import is `catalog/episodes.py` (`IS_SPECIAL`, `EPISODE_ORDER`) and
`catalog/seasons.py`.
"""

from uuid import UUID

from sqlalchemy import Select, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import (
    UserEpisodeRating,
    UserEpisodeWatch,
    UserShowRating,
    UserShowWatch,
)
from tvbf.catalog.models import Episode


def show_ids_with_a_record(user_id: UUID) -> Select[tuple[int]]:
    """Every show this user has a record for (project spec §8), as a select.

    `union_all` rather than `union`: the result is only ever used as a membership
    test or turned into a `frozenset`, so deduplication buys nothing and costs a
    hash aggregate over the two episode-grain branches, which are the ones that
    actually repeat.

    **Every branch selects a NOT NULL column**, which is what makes this safe as
    a `NOT IN` operand: one NULL in the subquery would make the anti-join return
    nothing at all, silently emptying the recommendations page. `show_id` is NOT
    NULL on all three `app` tables and on `catalog.episode`, and all four are
    foreign-keyed to `catalog.show`.
    """
    branches = union_all(
        select(UserShowWatch.show_id).where(UserShowWatch.user_id == user_id),
        select(UserShowRating.show_id).where(UserShowRating.user_id == user_id),
        select(Episode.show_id)
        .join(UserEpisodeWatch, UserEpisodeWatch.episode_id == Episode.id)
        .where(UserEpisodeWatch.user_id == user_id),
        select(Episode.show_id)
        .join(UserEpisodeRating, UserEpisodeRating.episode_id == Episode.id)
        .where(UserEpisodeRating.user_id == user_id),
    ).subquery()
    return select(branches.c.show_id)


async def load_show_ids_with_a_record(db: AsyncSession, *, user_id: UUID) -> frozenset[int]:
    """The same answer, materialised, for the payload builder.

    One query, replacing the `episode_rating_repo` call the builder made and the
    Python union it took with the taste signals' keys — so `build_payload`'s query
    count is unchanged, and the never-recommend set stops depending on
    `taste_for_user`'s universe happening to coincide with three of these four
    sources.
    """
    return frozenset((await db.scalars(show_ids_with_a_record(user_id))).all())
