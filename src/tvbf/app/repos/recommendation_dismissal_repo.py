"""Writes to `app.user_recommendation_dismissal` (NEU-1178).

One function, and that is the honest consequence of where the two halves of this
table live. The **read** is a branch of `recommendations/exclusion.py`, because
the never-recommend set is one sentence expressed once and used at both ends;
the **write** is here, because a rule module owning an HTTP-driven mutation is
how the next source's author decides to put theirs there too. The asymmetry is
not a mistake — it is exactly how the other four sources already work, with
`exclusion.py` selecting from `UserShowWatch` directly while
`show_membership_repo` owns the writes to it.

Its own file rather than `recommendation_repo.py`: that module spans two tables
because *"the current set" is one definition*, and a dismissal is not part of any
set. It is a fact about the user that outlives every set they will be given —
the endpoint does not even look at the current set — so folding it in would make
that module's opening sentence false about a third of its contents.
"""

from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import UserRecommendationDismissal


async def add(db: AsyncSession, *, user_id: UUID, show_id: int) -> None:
    """Idempotent INSERT … ON CONFLICT DO NOTHING, on `show_membership_repo.add`'s shape.

    The conflict target is the composite primary key, so dismissing the same show
    twice leaves one row and neither call has to ask first. The caller commits.
    """
    stmt = pg_insert(UserRecommendationDismissal).values(user_id=user_id, show_id=show_id)
    stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "show_id"])
    await db.execute(stmt)
