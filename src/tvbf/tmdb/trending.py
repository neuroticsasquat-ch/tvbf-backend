"""The daily `/trending/tv/week` snapshot (NEU-1055).

Trending is the only discovery surface that still calls an endpoint on a
schedule (project spec §3), and that is a measurement rather than an oversight:
it is a velocity signal over view and search counts we do not hold, and
demonstrably not a popularity sort — *Our Sticky Love* (popularity 165) ranks
below *Lanterns* (popularity 56) in the live list. A home-grown approximation
from seven-day popularity deltas became constructible once NEU-1172 started
refreshing popularity nightly, but it would be an unvalidatable tuning project
undertaken to save one request a day.

## `week`, not `day`

Daily trending is news-reactive — one cast controversy or one viral clip spikes
a show for twenty-four hours. This job runs daily either way, so `day` would buy
volatility rather than freshness. `week` matches the decision the surface
actually feeds, which is "should I start this?", and that plays out over weeks.

## Four rules, each of which this pass would be wrong without

**The snapshot is replaced whole, inside one transaction.** TMDB's ranking is a
total order and a partial update would interleave two vintages of it — the same
reason `show_recommendation` replaces a source show's list rather than merging
it. Under MVCC a concurrent reader sees the previous list or the new one, never
a half-written mix.

**`captured_at` is stamped before the request goes out**, so it describes the
list rather than the bookkeeping that stored it. It is what NEU-1056's seven-day
staleness cutoff is measured against, and a write-time stamp would quietly
credit the snapshot with however long resolution took.

**An entry that does not resolve to a `catalog.show` is dropped, and logged.**
The project-wide rule is that if a user cannot click it and add it to My Shows
it does not appear, and measured this is a no-op — 20 of 20 trending ids
resolved, which is what a TMDB spine buys on the twenty most globally popular
titles there are. It stays as a guard because a series TMDB created this morning
is not mirrored until tonight's delta, and it logs at `error` because a nonzero
count means something is wrong with the ingest rather than with trending.

**Resolving *nothing* writes nothing.** The acceptance criterion says a failed
run leaves the previous snapshot intact rather than truncating, and a run whose
every entry was dropped is a failed run wearing a 200 — replacing yesterday's
usable list with an empty one on that evidence is the one outcome worse than
serving a day-old list. The run is finalized `failed` so Coolify and the deadman
both hear about it.
"""

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.catalog import models as m
from tvbf.catalog.runs import finalize_run, record_progress
from tvbf.config import Settings
from tvbf.db import SessionLocal
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.ingest import SessionFactory, _owned_session

log = logging.getLogger(__name__)

# The window this product asks for. See the module docstring: the job runs daily
# whichever is chosen, so `day` buys volatility rather than freshness.
TRENDING_WINDOW = "week"


@dataclass(frozen=True)
class TrendingResult:
    """What one snapshot did.

    `offered` is what TMDB listed and `stored` what survived. The two ways an
    entry is lost are counted **separately and never summed into one number**,
    because they mean opposite things: `unresolved` is a series TMDB named and
    this catalog does not hold, which is an ingest defect and is what
    `ingest_run.shows_failed` records, while `duplicated` is TMDB listing one
    show twice, which is upstream tidiness and nobody's failure.

    `skipped_reason` is set only when the previous snapshot was deliberately
    left standing, which is the one outcome that writes nothing at all.
    """

    stored: int
    offered: int
    unresolved: int
    duplicated: int
    captured_at: datetime | None = None
    skipped_reason: str | None = None


def ranked_series_ids(body: dict) -> Iterator[tuple[int, int]]:
    """`(rank, series id)` for each usable entry of a `/trending/tv` page.

    Rank is the entry's 1-based position in TMDB's response and is taken from
    the position rather than from a counter over the yielded rows, so a
    malformed entry leaves a gap exactly as an unresolvable one does.

    A malformed entry is skipped rather than fatal, on `_series_ids_in`'s
    reasoning one module over: this is somebody else's feed, and losing one
    entry from a list of twenty beats losing the day's snapshot. That is also
    why the page is read as a dict rather than through a Pydantic shape — a
    strict parse of a namespace we take one field from would fail the run over a
    field nothing here reads.
    """
    for rank, row in enumerate(body.get("results") or [], start=1):
        series_id = row.get("id") if isinstance(row, dict) else None
        if isinstance(series_id, int) and not isinstance(series_id, bool):
            yield rank, series_id


async def _resolve(session: AsyncSession, tmdb_ids: Sequence[int]) -> dict[int, int]:
    """`tmdb_id -> catalog.show.id` for the ids this catalog holds.

    Existence and nothing else. `adult` and `deleted_upstream_at` are read-time
    filters (NEU-1053, NEU-1108) — applying them here would make a resurrected
    show unrecoverable until the next snapshot, and would confuse "TMDB named a
    series we have not mirrored", which is an ingest defect worth logging, with
    "we hold it and choose not to show it", which is not.
    """
    if not tmdb_ids:
        return {}
    rows = await session.execute(
        select(m.Show.tmdb_id, m.Show.id).where(m.Show.tmdb_id.in_(tuple(tmdb_ids)))
    )
    return {tmdb_id: show_id for tmdb_id, show_id in rows.all() if tmdb_id is not None}


async def replace_snapshot(
    session: AsyncSession, *, ranked: Sequence[tuple[int, int]], captured_at: datetime
) -> TrendingResult:
    """Swap the stored snapshot for this one. Caller owns the transaction.

    A show TMDB listed twice keeps its first — best — rank and the duplicate is
    counted as dropped. `uq_trending_show_show` would refuse the second row
    anyway; deduplicating here is what makes that constraint a statement about
    the data rather than a way for a run to die.
    """
    by_tmdb = await _resolve(session, [tmdb_id for _, tmdb_id in ranked])

    rows: list[dict[str, object]] = []
    seen: set[int] = set()
    unresolved: list[int] = []
    duplicated = 0
    for rank, tmdb_id in ranked:
        show_id = by_tmdb.get(tmdb_id)
        if show_id is None:
            unresolved.append(tmdb_id)
            continue
        if show_id in seen:
            log.warning("TMDB listed series %d twice in one trending page", tmdb_id)
            duplicated += 1
            continue
        seen.add(show_id)
        rows.append({"rank": rank, "show_id": show_id, "captured_at": captured_at})

    if unresolved:
        log.error(
            "%d of %d trending series are not mirrored and were dropped: %s "
            "— this is an ingest problem, not a trending one",
            len(unresolved),
            len(ranked),
            unresolved,
        )

    if not rows:
        reason = f"no trending entry of {len(ranked)} resolved to a mirrored show"
        log.error("trending snapshot wrote nothing, previous snapshot left intact: %s", reason)
        return TrendingResult(0, len(ranked), len(unresolved), duplicated, skipped_reason=reason)

    await session.execute(delete(m.TrendingShow))
    await session.execute(insert(m.TrendingShow).values(rows))
    return TrendingResult(
        len(rows), len(ranked), len(unresolved), duplicated, captured_at=captured_at
    )


async def run_trending_snapshot(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    run_id: UUID,
) -> TrendingResult:
    """One request, one transaction, one finalized run."""
    # Before the request, not after: this timestamp describes the list TMDB is
    # about to hand over, and the staleness cutoff downstream reads it as such.
    captured_at = datetime.now(UTC)
    body = await client.get_trending_tv(window=TRENDING_WINDOW)
    ranked = list(ranked_series_ids(body))
    log.info("trending %s: %d entries captured at %s", TRENDING_WINDOW, len(ranked), captured_at)

    async with _owned_session(session_factory) as s:
        result = await replace_snapshot(s, ranked=ranked, captured_at=captured_at)
        # `shows_failed` takes the unresolved count alone. A duplicate is not a
        # failure of anything and putting it here would corrupt the one durable
        # record of the number this ticket asks an operator to read as an ingest
        # defect.
        await record_progress(
            s, run_id, processed_delta=result.stored, failed_delta=result.unresolved
        )
        if result.skipped_reason is not None:
            await finalize_run(s, run_id, status="failed", error=result.skipped_reason)
        else:
            await finalize_run(s, run_id, status="succeeded")
        await s.commit()

    log.info(
        "trending snapshot: stored %d of %d offered (%d unmirrored, %d duplicated)",
        result.stored,
        result.offered,
        result.unresolved,
        result.duplicated,
    )
    return result


def _session_factory():
    return SessionLocal()


async def run_trending_snapshot_job(run_id: UUID, settings: Settings) -> None:
    """One snapshot, wired from settings and guaranteed to finalize.

    The shape `run_catalog_update_job` and `run_airdate_reconcile_job` already
    have, and for the same reason: the scheduled entrypoint awaits this, and
    anything escaping it would leave a `running` row for the stale-run cleanup
    to find hours later.
    """
    try:
        async with TMDBClient(
            base_url=settings.tmdb_base_url,
            read_access_token=settings.tmdb_read_access_token,
            rate_calls=settings.tmdb_rate_limit_requests,
            rate_window=settings.tmdb_rate_limit_window_seconds,
            retry_max_attempts=settings.tmdb_retry_max_attempts,
        ) as client:
            await run_trending_snapshot(
                session_factory=_session_factory, client=client, run_id=run_id
            )
    except Exception as e:
        log.exception("trending snapshot crashed")
        async with SessionLocal() as s:
            await finalize_run(s, run_id, status="failed", error=str(e))
            await s.commit()
