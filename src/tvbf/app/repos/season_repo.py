from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.catalog import seasons as season_rules
from tvbf.catalog.models import Episode, Season


async def unaired_for_shows(db: AsyncSession, show_ids: list[int], today: date) -> list[Season]:
    """Return Season rows for the given shows whose episodes have all not
    aired yet — i.e., no episode in the season has a non-null air date on or
    before `today`. Includes seasons with no episodes defined and seasons
    whose episodes all have null air dates. Used by the Upcoming Seasons
    endpoint (NEU-135).

    Two details are load-bearing, and they guard the same failure from opposite
    sides — a show carrying both an ingested season and a copied one for the
    same number, which is what `catalog/seasons.py` exists for.

    **Deduplicated before the unaired filter, not after.** Filtering first would
    leave only the copy in the candidate set when the ingested row has aired, and
    the season would surface as upcoming on the strength of a row the read path
    does not show anywhere else.

    **"Has it aired" is asked per season *number*, not per season id.** The
    episodes of a doubled season can hang off either row — a delta creates a
    fresh duplicate and writes its episodes under the new one — so asking the
    surviving row for *its own* episodes reports an already-aired season as
    episode-less. The episode's denormalised `season_number` is the same grain
    `episode_repo.aired_count_per_season` already groups by.
    """
    if not show_ids:
        return []
    canonical = season_rules.deduped(
        (await db.execute(select(Season).where(Season.show_id.in_(show_ids)))).scalars().all()
    )
    if not canonical:
        return []

    aired = set(
        (
            await db.execute(
                select(Episode.show_id, Episode.season_number)
                .where(
                    Episode.show_id.in_(show_ids),
                    Episode.air_date.is_not(None),
                    Episode.air_date <= today,
                )
                .distinct()
            )
        ).all()
    )
    return [s for s in canonical if (s.show_id, s.season_number) not in aired]
