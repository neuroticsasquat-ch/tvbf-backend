from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze.models import Episode, Season


async def unaired_for_shows(db: AsyncSession, show_ids: list[int], today: date) -> list[Season]:
    """Return Season rows for the given shows whose episodes have all not
    aired yet — i.e., no episode in the season has a non-null airdate on or
    before `today`. Includes seasons with no episodes defined and seasons
    whose episodes all have null airdates. Used by the Upcoming Seasons
    endpoint (NEU-135)."""
    if not show_ids:
        return []
    # Exclude season_ids that have at least one already-aired episode. The
    # `season_id IS NOT NULL` guard prevents NULL contamination of the NOT IN
    # set (NOT IN against a NULL result yields UNKNOWN for every row).
    aired_subq = (
        select(Episode.season_id)
        .where(
            Episode.show_id.in_(show_ids),
            Episode.season_id.is_not(None),
            Episode.airdate.is_not(None),
            Episode.airdate <= today,
        )
        .distinct()
    )
    stmt = select(Season).where(
        Season.show_id.in_(show_ids),
        Season.id.not_in(aired_subq),
    )
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)
