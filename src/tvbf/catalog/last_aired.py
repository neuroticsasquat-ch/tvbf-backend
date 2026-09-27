"""`catalog.show.last_aired`: the one recompute, and who calls it (NEU-1502).

**Last aired** (CONTEXT.md) is the air date of a show's most recent *regular*
episode on or before today, `NULL` when there is none. Browse used to compute it
per row in `ORDER BY` — a correlated `max()` over 6.6M episodes, 4.7 s for an
unfiltered page — so it is stored on the spine instead, on the precedent of
`show.runtime`, and kept true by four callers:

1. `tmdb/upsert.py::upsert_series_payload`, beside `refresh_runtime` — the path
   through which the full pass and the delta write episodes.
2. `catalog/offsets.py::project_offsets` — the only other writer of
   `episode.air_date`.
3. `tmdb/update.py::run_catalog_update`, over the whole catalog: the **daily
   roll-forward**. An episode crosses `air_date <= today` without any row
   changing, so nothing else would move yesterday's premiere up the sort.
4. The migration's backfill, which holds an inline copy of this statement
   because no migration imports application code.

`today` is always bound in by the caller as the server's UTC date — the jobs'
convention — never `current_date`, whose answer depends on the connection's
timezone. The My Shows surfaces deliberately keep their live
`episode_repo.latest_aired_per_show` with the viewer's own `?today=`; the two
share the definition, including the specials rule, not the read path.
"""

from collections.abc import Sequence
from datetime import date

from sqlalchemy import func, not_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from tvbf.catalog import models as m
from tvbf.catalog.episodes import IS_SPECIAL


async def recompute_last_aired(
    session: AsyncSession, *, today: date, show_ids: Sequence[int] | None = None
) -> int:
    """Set `last_aired` from the episode rows; returns the number of shows changed.

    Scoped to `show_ids` when given, the whole catalog when `None`. One
    statement: every in-scope show is `LEFT JOIN`ed to its aggregate, so a show
    whose last qualifying episode went away is nulled rather than left stale, and
    `IS DISTINCT FROM` writes only the rows that moved — the nightly run over
    231k shows touches the handful whose date actually changed.

    The caller owns the transaction.
    """
    latest = (
        select(m.Episode.show_id, func.max(m.Episode.air_date).label("last_aired"))
        .where(m.Episode.air_date <= today, not_(IS_SPECIAL))
        .group_by(m.Episode.show_id)
    )
    scope = aliased(m.Show)
    in_scope = select(scope.id)
    if show_ids is not None:
        # Narrow the aggregate as well as the target, or a one-show recompute
        # would aggregate every episode in the catalog to use one group.
        latest = latest.where(m.Episode.show_id.in_(show_ids))
        in_scope = in_scope.where(scope.id.in_(show_ids))
    agg = latest.subquery()
    target = in_scope.add_columns(agg.c.last_aired).outerjoin(agg, agg.c.show_id == scope.id)
    rows = target.subquery()
    result = await session.execute(
        update(m.Show)
        .where(m.Show.id == rows.c.id)
        .where(m.Show.last_aired.is_distinct_from(rows.c.last_aired))
        .values(last_aired=rows.c.last_aired)
    )
    return result.rowcount  # type: ignore[attr-defined]
