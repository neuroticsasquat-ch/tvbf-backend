"""The oracle's id for one show, cached so the pass stops re-deriving it (NEU-1148).

`airdates/reconcile` spent **two** TV Maze requests per show: a `/lookup/shows`
by external id, then the episode list. The first is pure re-derivation — the
same external ids produce the same answer every night, and we threw the answer
away every night. The pass is entirely rate-limiter-bound (measured at ~1.13
s/show against a budget of 18 requests per 10 seconds, which is exactly two
requests), so halving the requests roughly halves the ~23 minute wall clock.

**Three cache states, and the middle one is why the table exists.**

| state | meaning | what happens |
| -- | -- | -- |
| no row | never asked | look up, write the result |
| row, `tvmaze_id` set | resolved | reuse it, spend no lookup |
| row, `tvmaze_id` NULL | asked, no counterpart | reuse the negative until it expires |

Without the negative cache the ~500 shows in scope TV Maze has never heard of
would be re-looked-up every night forever, which is most of the saving gone.
`resolved_at` is what dates the negative — the same distinction
`catalog.show.credits_synced_at` exists to make one grain up, where "the show has
no `show_cast` row" could not tell *upstream has none* from *nobody asked* and so
would never converge.

**A resolved id is never re-looked-up on a timer**; `RELOOKUP_MISSING_AFTER`
expires only a negative. What handles an id that stops working is the
invalidation path below, and the asymmetry is the point: a timer over resolved
ids would spend back exactly the requests this module exists to save.

**A stale link is invalidated and retried once, not left to fail silently.**
Before the cache, a show whose TV Maze entry disappears resolved to `None` every
night and was logged by name. With a cached id we stop asking, and the episode
fetch 404s instead — which, if it read as an empty list, would make every season
judge `no_overlap`, leave every offset alone by design, and end the show's
reconciliation forever while counting only toward a number that is already large
and ordinary. So `get_show_episodes` answers `None` for a 404 and `[]` for a show
upstream genuinely carries with no episodes, and on `None` this module clears the
link, re-resolves once and re-fetches: three requests for that show that night,
one thereafter.

**The module both reads a table and calls the client, and is deliberately not a
pure repository.** The cache exists *only* to avoid a request, so a module that
knew about the table but not the request would be a seam in the wrong place and
the orchestration would leak back into `_reconcile_show`, whose subject is the
trust rule.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.airdates.api_payloads import TVMazeEpisode
from tvbf.airdates.client import TVMazeOracleClient
from tvbf.catalog import models as m

log = logging.getLogger(__name__)

# How long a negative — "we asked and TV Maze does not carry this show" — is
# reused before we ask again. A negative is not permanent: a show TV Maze adds
# later should eventually be found.
#
# **A module constant, not a setting**, beside `MAX_OFFSET_DAYS` and
# `MIN_EPISODES` in `reconcile.py`, which are the precedent: rules about what
# the pass believes, changed by a code change with a test rather than by an
# operator. There is no scenario where prod and dev should disagree about it,
# unlike `INGEST_STALE_RUN_MINUTES`, whose value depends on deploy cadence.
# Thirty days is ample — this population is specifically shows nobody can
# currently correct by any means, so a month's latency on one appearing costs
# nothing.
RELOOKUP_MISSING_AFTER = timedelta(days=30)


class SpendCounters(Protocol):
    """What this module reports about the requests it made or avoided.

    A structural type rather than an import, so the counters can live flat on
    `ReconcileResult` — which is what the closing log line reads — without this
    module importing the pass that calls it. `lookups_spent` falling to near
    zero across consecutive runs is the acceptance criterion, observable in
    production rather than only in a test.
    """

    lookups_spent: int
    links_reused: int
    links_invalidated: int


@dataclass(frozen=True)
class _Link:
    tvmaze_id: int | None
    resolved_at: datetime


async def _load_link(session: AsyncSession, show_id: int) -> _Link | None:
    row = (
        await session.execute(
            select(m.AirdateShowState.tvmaze_id, m.AirdateShowState.resolved_at).where(
                m.AirdateShowState.show_id == show_id
            )
        )
    ).one_or_none()
    return None if row is None else _Link(row.tvmaze_id, row.resolved_at)


async def _record_link(session: AsyncSession, show_id: int, tvmaze_id: int | None) -> None:
    """Write what the oracle just answered, replacing whatever was there.

    Upsert rather than insert-or-update by hand: a re-resolution after an
    expired negative and one after an invalidated id are the same write, and
    `resolved_at` moves either way because its meaning is "when we last asked".
    """
    await session.execute(
        insert(m.AirdateShowState)
        .values(show_id=show_id, tvmaze_id=tvmaze_id, resolved_at=datetime.now(UTC))
        .on_conflict_do_update(
            index_elements=[m.AirdateShowState.show_id],
            set_={"tvmaze_id": tvmaze_id, "resolved_at": datetime.now(UTC)},
        )
    )


def _usable(link: _Link, relookup_missing_after: timedelta) -> bool:
    """Whether a stored answer may stand in for a request tonight.

    A resolved id always may. A negative may until it expires — compared against
    this process's clock rather than the database's, which differ by
    milliseconds against an interval of thirty days.
    """
    if link.tvmaze_id is not None:
        return True
    return datetime.now(UTC) - link.resolved_at < relookup_missing_after


class _ShowIdentity(Protocol):
    """The four fields of a show this module needs. `ShowToCheck` satisfies it.

    Structural and read-only — properties rather than attributes, which is what
    a frozen dataclass satisfies — so the dependency points from the pass to its
    bookkeeping and never back. Importing `ShowToCheck` would be a cycle, since
    `reconcile` imports this module.
    """

    @property
    def show_id(self) -> int: ...

    @property
    def name(self) -> str: ...

    @property
    def imdb_id(self) -> str | None: ...

    @property
    def tvdb_id(self) -> int | None: ...


async def oracle_episodes(
    session: AsyncSession,
    client: TVMazeOracleClient,
    show: _ShowIdentity,
    counters: SpendCounters,
    *,
    relookup_missing_after: timedelta = RELOOKUP_MISSING_AFTER,
) -> list[TVMazeEpisode] | None:
    """The oracle's episodes for one show, spending a lookup only if it must.

    `None` means TV Maze has no counterpart for this show — whether we learned
    that just now or from a row written up to `relookup_missing_after` ago. `[]`
    means a counterpart that carries no episodes.

    The caller gets one call and never learns a cache exists. Exposing
    load/record/clear instead would push five branches of caching policy into
    `_reconcile_show`, whose subject is the trust rule, and put the retry-once
    loop somewhere a test could only reach through a full pass.
    """
    link = await _load_link(session, show.show_id)

    if link is not None and _usable(link, relookup_missing_after):
        counters.links_reused += 1
        if link.tvmaze_id is None:
            return None
        episodes = await client.get_show_episodes(link.tvmaze_id)
        if episodes is not None:
            return episodes
        # The link has gone stale: TV Maze no longer serves that id. Counted
        # rather than merely retried, so a run quietly re-resolving many shows
        # is visible instead of only slow.
        counters.links_invalidated += 1
        log.info(
            "show %d (%s): TV Maze id %d no longer resolves — re-looking it up",
            show.show_id,
            show.name,
            link.tvmaze_id,
        )

    tvmaze_id = await client.lookup_show(imdb_id=show.imdb_id, tvdb_id=show.tvdb_id)
    counters.lookups_spent += 1
    await _record_link(session, show.show_id, tvmaze_id)
    if tvmaze_id is None:
        return None
    return await client.get_show_episodes(tvmaze_id)
