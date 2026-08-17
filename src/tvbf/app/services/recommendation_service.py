"""What `GET /me/recommendations` serves (NEU-1112).

The read is `recommendation_repo.list_current_recommendations` and nothing else:
"the set a user is currently seeing" is defined once (NEU-1108) so the weekly
pass and this surface cannot disagree about it, and a fresh query here would be
the second implementation that module exists to prevent. Everything below is
what a *surface* decides on top of that answer — how many rows to show, and how
to shape them.

**The cap lives here, on the server.** The client never slices: the moment it
did, the two would disagree about what "twelve" means the first time a tombstone
landed, because the read-time `adult` / `deleted_upstream_at` filters (project
spec §8) run before the cap and not after it. Twelve *survivors* out of the
twenty-five asked for is exactly what the headroom in §7 is for — a set generated
in March can name a show tombstoned in June, and the surface should still be
full.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.repos import recommendation_repo
from tvbf.app.schemas import RecommendationOut, RecommendationsOut
from tvbf.app.services.my_shows_service import build_show_summary_from_refs
from tvbf.catalog.browse_queries import hydrate_show_refs

DISPLAY_LIMIT = 12
"""Project spec §11: twelve cards, out of the twenty-five the model is asked for."""


async def list_recommendations(db: AsyncSession, *, user_id: UUID) -> RecommendationsOut:
    """The current set's top `DISPLAY_LIMIT` surviving suggestions, in rank order.

    A user with no succeeded set gets an empty list rather than an error or a
    204 — never having run, having run and resolved nothing, and having failed
    are three states the repo already collapses into one answer for readers, and
    the surface renders all three the same way: no section.

    The rows arrive ordered by rank and are never re-sorted; the slice is taken
    off the front of that order, so it is the model's own top twelve.

    `hydrate_my_ratings` is deliberately not called, unlike every other `/me`
    list route, so `ShowSummary.my_rating` is always null here. A show the user
    has rated is a show they have a record for, and §8's never-recommend filter
    is the union of exactly those records — so a non-null value would mean the
    exclusion had failed rather than that the field was worth filling. Adding
    the query would spend a round trip to display nothing.
    """
    rows = (await recommendation_repo.list_current_recommendations(db, user_id=user_id))[
        :DISPLAY_LIMIT
    ]
    shows = [show for _rec, show in rows]
    genres_by_show, networks_by_show = await hydrate_show_refs(db, shows)
    return RecommendationsOut(
        recommendations=[
            RecommendationOut(
                **build_show_summary_from_refs(
                    show,
                    genres_by_show=genres_by_show,
                    networks_by_show=networks_by_show,
                ).model_dump(),
                rank=rec.rank,
            )
            for rec, show in rows
        ]
    )
