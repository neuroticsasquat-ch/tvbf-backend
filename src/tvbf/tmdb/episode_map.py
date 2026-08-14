"""Attach `tmdb_id` to the copied episode rows, and report what would not map (NEU-1045).

NEU-1043 mapped the show grain. This is the same job one level down, and it is
where the migration's risk actually lives: show grain leaves six rows of residue
in production, episode grain is **7,071 watched episodes across two catalogues
that disagree** about specials, season numbering, and where a two-parter is one
episode or two.

## Why the ids have to be attached at all

`catalog.episode` upserts conflict-target `tmdb_id` (ADR-0008), and a copied row
carries none. So when the full ingest (NEU-1034) reaches a show the migration
matched, its episode upsert finds nothing to conflict with and **inserts a second
row for every episode** — one holding TMDB data with a fresh surrogate, one
holding TV Maze data with the preserved id `app.user_episode_watch` points at.
Stamping the copied row first is what collapses those two into the row users are
already attached to: the ingest then updates it in place, the watch record never
moves, and no episode is listed twice.

That is why the pass is **run after `enrich:tmdb-ids` and before the ingest**, the
same slot and the same reason as NEU-1044's human queue. Run it afterwards and
every upstream id it finds is already held by an ingested row, so the write is
refused and counted as a collision — informative, but far too late to be the
plan.

## What it does not do

**Seasons are deliberately out of scope.** The copied `catalog.season` rows carry
no `tmdb_id` either, so the ingest inserts its own alongside them, and the episode
upsert re-points these episodes at the new season row by number. Nothing in `app`
references a season, so no user data rides on it — it is a duplicate-row problem
of exactly the shape NEU-1066 owns at show grain, and it is called out here
rather than fixed here.

**Nothing in `app` is written, read-for-ordering aside.** A watch record's
episode is already a valid `catalog.episode` row whatever happens below; an
unmatched episode keeps its TV Maze data and its `tmdb_id IS NULL` forever, which
is the sanctioned locally-authored row rather than a failure.

## Matching, and every way it declines to guess

One key: `(season_number, episode_number)`, taken from the same series payload
the ingest fetches. There is no title comparison and no air-date tolerance — at
episode grain a wrong id silently re-labels somebody's watch history, and the
cheap alternative (leave it unmapped, list it in the report) costs a row on a
report that is already being read by hand.

Four outcomes, three of which are not a match, all counted per show:

* **matched** — exactly one local row for a key upstream also has.
* **ambiguous** — more than one local row carries that key, so which of them the
  upstream episode *is* has no answer. TV Maze carries 2,298 duplicate
  `(show, season, number)` triples across 13 shows that number two seasons the
  same (NEU-1042), and this is where they land.
* **collision** — the upstream id is already held by another `catalog.episode`.
  Post-ingest that is every episode of a copied show; pre-ingest it is a genuine
  duplicate and worth reading.
* **unmatched** — upstream has no episode at that key. Splits, merges and
  renumberings live here, and so does every synthetic special (below), which is
  counted apart because it can never be anything else.

**A negative `episode_number` is never mappable.** The copy synthesised -1, -2, …
for TV Maze's null-numbered specials (27,498 rows in prod, 156 watched), and TMDB
numbers its specials positively inside season 0. There is no key to match on, by
construction, so those rows are excluded from the work list rather than retried
every run — see `_HAS_MAPPABLE_EPISODE`.

## Re-running

There is no watermark column, for the same reason `enrichment.py` needs none: a
row leaves the work list by being mapped. A show still holding a mappable
unmapped episode is reconsidered on the next run, which is the cheap way to pick
up an episode TMDB has since added, and a show whose residue is nothing but
synthetic specials drops out on its own.

The cost of that choice is the one it always is — a show whose episodes
genuinely cannot map is re-fetched on every run. At one request per show that is
the right side of the trade against a column this migration would then have to
carry, and drop.
"""

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tmdb.api_payloads import TMDBSeasonDetail
from tvbf.tmdb.client import TMDBClient, is_gone_upstream
from tvbf.tmdb.ingest import fetch_series_with_seasons

log = logging.getLogger(__name__)

# Shows per commit. The pass is one request per show over the ~63k the migration
# matched, so like `enrichment.py` it has to checkpoint: work that cost upstream
# calls must not be thrown away by a crash. Smaller than enrichment's 500 because
# a show here writes as many rows as it has episodes rather than one.
_BATCH_SIZE = 200

# Consecutive per-show failures before the pass gives up. A 404 does not count —
# a series id the mapping attached and `/tv/{id}` no longer serves is a data
# condition, not a broken upstream (NEU-1006), and the show simply stays
# unmapped.
_FAILURE_THRESHOLD = 10

# Episodes below this number were invented by the copy for TV Maze's
# null-numbered specials and have no upstream counterpart to match. Real episode
# numbers start at 1; season 0 is where TMDB puts its specials, with numbers of
# their own, so the boundary is the sign rather than the season.
_LOWEST_MAPPABLE_NUMBER = 0


class EpisodeMapAborted(Exception):
    """Too many consecutive per-show failures. Ends the pass; the log has the rest."""


@dataclass(frozen=True)
class ShowToMap:
    """A matched show with episodes still to map. Not the ORM row — this is all of it."""

    id: int
    tmdb_id: int
    name: str


@dataclass(frozen=True)
class ShowMapResult:
    """What one show's episodes did. Every local row lands in exactly one count."""

    matched: int = 0
    unmatched: int = 0
    ambiguous: int = 0
    collisions: int = 0
    # Synthetic specials — negative-numbered, unmappable by construction, and
    # kept apart from `unmatched` so a report reader is never asked to wonder
    # whether they are a mapping failure.
    synthetic: int = 0

    def __add__(self, other: "ShowMapResult") -> "ShowMapResult":
        return ShowMapResult(
            matched=self.matched + other.matched,
            unmatched=self.unmatched + other.unmatched,
            ambiguous=self.ambiguous + other.ambiguous,
            collisions=self.collisions + other.collisions,
            synthetic=self.synthetic + other.synthetic,
        )


@dataclass(frozen=True)
class EpisodeMapResult:
    shows_considered: int
    shows_failed: int
    shows_gone: int
    episodes: ShowMapResult


# What makes a show worth a request: it is matched, and it still holds an episode
# that *could* map. Written once and shared by the work list and its count, the
# way `enrichment._NOT_HUMAN` is — the denominator in the progress log means
# nothing if it drifts from what the loop actually takes.
_HAS_MAPPABLE_EPISODE = f"""
    s.tmdb_id IS NOT NULL
    AND EXISTS (
        SELECT 1
          FROM catalog.episode e
         WHERE e.show_id = s.id
           AND e.tmdb_id IS NULL
           AND e.episode_number >= {_LOWEST_MAPPABLE_NUMBER}
    )
"""

# Keyset paging rather than OFFSET, exactly as `enrichment.py` does it: a show
# leaves the candidate set the moment its episodes are written, so an offset
# would step over whatever slid into its place.
_CANDIDATES = text(f"""
    SELECT s.id, s.tmdb_id, s.name
      FROM catalog.show s
     WHERE {_HAS_MAPPABLE_EPISODE}
       AND s.id > :after_id
     ORDER BY s.id
     LIMIT :limit
""")

_REMAINING = text(f"SELECT count(*) FROM catalog.show s WHERE {_HAS_MAPPABLE_EPISODE}")

# Every episode of one show, mapped or not: an already-mapped row is what makes a
# key ambiguous, so the ambiguity check cannot be run over the unmapped ones
# alone.
_LOCAL_EPISODES = text("""
    SELECT id, season_number, episode_number, tmdb_id
      FROM catalog.episode
     WHERE show_id = :show_id
     ORDER BY id
""")

# One statement per show, with both guards from `enrichment._ATTACH` restated at
# the write where the guarantee belongs.
#
# `tmdb_id IS NULL` is what stops a re-run rewriting a row that already matched.
# `NOT EXISTS` is the collision check: `uq_episode_tmdb_id` would otherwise raise
# and take the whole batch's transaction with it, losing shows that had nothing
# to do with the duplicate. It covers this row too, harmlessly — its `tmdb_id` is
# null by the line above.
#
# `unnest` of two arrays rather than a `VALUES` list so one bind parameter pair
# carries a show of any length: a soap runs to thousands of episodes, and
# Postgres caps a statement at 32,767 bind parameters — the same ceiling that
# forces `_EPISODE_BATCH_SIZE` next door.
_ATTACH = text("""
    UPDATE catalog.episode e
       SET tmdb_id = v.tmdb_id
      FROM unnest(cast(:ids AS bigint[]), cast(:tmdb_ids AS integer[])) AS v(id, tmdb_id)
     WHERE e.id = v.id
       AND e.tmdb_id IS NULL
       AND NOT EXISTS (
           SELECT 1 FROM catalog.episode other WHERE other.tmdb_id = v.tmdb_id
       )
 RETURNING e.id
""")


def _upstream_episode_ids(
    season_details: Sequence[TMDBSeasonDetail],
) -> dict[tuple[int, int], int]:
    """`{(season_number, episode_number): tmdb episode id}` for one series.

    Two kinds of duplicate are dropped rather than resolved, and both are
    upstream saying something we must not answer with a guess: a key carrying two
    different episode ids, and one episode id arriving under two keys. The second
    also protects the write — two rows in one `UPDATE` set to the same
    `tmdb_id` would violate `uq_episode_tmdb_id` and kill the batch.

    A season fetched twice (appended *and* as an overflow) is not a duplicate:
    the key and the id agree, so the second sighting is simply the same fact.
    """
    by_key: dict[tuple[int, int], int] = {}
    contested: set[tuple[int, int]] = set()
    keys_by_id: dict[int, set[tuple[int, int]]] = defaultdict(set)

    for detail in season_details:
        for episode in detail.episodes:
            key = (episode.season_number, episode.episode_number)
            keys_by_id[episode.tmdb_id].add(key)
            if by_key.setdefault(key, episode.tmdb_id) != episode.tmdb_id:
                contested.add(key)

    for tmdb_id, keys in keys_by_id.items():
        if len(keys) > 1:
            log.warning("TMDB episode %d appears under %d keys — not mapped", tmdb_id, len(keys))
            contested |= keys

    for key in contested:
        by_key.pop(key, None)
    return by_key


@dataclass(frozen=True)
class _LocalEpisode:
    id: int
    season_number: int
    episode_number: int
    tmdb_id: int | None

    @property
    def key(self) -> tuple[int, int]:
        return (self.season_number, self.episode_number)

    @property
    def mappable(self) -> bool:
        return self.episode_number >= _LOWEST_MAPPABLE_NUMBER


async def _local_episodes(session: AsyncSession, show_id: int) -> list[_LocalEpisode]:
    rows = (await session.execute(_LOCAL_EPISODES, {"show_id": show_id})).all()
    return [
        _LocalEpisode(
            id=row.id,
            season_number=row.season_number,
            episode_number=row.episode_number,
            tmdb_id=row.tmdb_id,
        )
        for row in rows
    ]


async def map_show_episodes(
    session: AsyncSession, client: TMDBClient, show: ShowToMap
) -> ShowMapResult:
    """Map one show's copied episodes onto TMDB ids, and report every outcome.

    Fetches through the ingest's own `fetch_series_with_seasons`, so a show whose
    seasons overflow `append_to_response` is mapped from the same complete
    payload the ingest would mirror — and an overflow fetch that fails takes the
    show down rather than mapping half of it, which is the behaviour that
    function already owns.

    **With no appended namespaces**, though: episode ids are all this pass reads,
    so the audit's eleven namespaces would be a show's whole credit list,
    translations and images fetched 63,000 times and discarded. Spending that
    budget on seasons instead widens the speculative window from `0..8` to
    `0..19`, which is the difference between one request and two for a
    long-running show.
    """
    series, overflow = await fetch_series_with_seasons(client, show.tmdb_id, namespaces=())
    upstream = _upstream_episode_ids([*series.appended_seasons, *overflow])

    local = await _local_episodes(session, show.id)
    rows_by_key: dict[tuple[int, int], list[_LocalEpisode]] = defaultdict(list)
    for episode in local:
        rows_by_key[episode.key].append(episode)

    pairs: list[tuple[int, int]] = []
    unmatched = 0
    ambiguous = 0
    synthetic = 0
    for episode in local:
        if episode.tmdb_id is not None:
            continue
        if not episode.mappable:
            synthetic += 1
            continue
        if len(rows_by_key[episode.key]) > 1:
            ambiguous += 1
            continue
        upstream_id = upstream.get(episode.key)
        if upstream_id is None:
            unmatched += 1
            continue
        pairs.append((episode.id, upstream_id))

    matched = 0
    if pairs:
        result = await session.execute(
            _ATTACH,
            {"ids": [local_id for local_id, _ in pairs], "tmdb_ids": [t for _, t in pairs]},
        )
        matched = len(result.all())

    collisions = len(pairs) - matched
    if collisions:
        log.warning(
            "show %d (%s): %d episode(s) matched a TMDB id another catalog row already holds",
            show.id,
            show.name,
            collisions,
        )
    if ambiguous:
        log.warning(
            "show %d (%s): %d episode(s) share a (season, number) with another row — not mapped",
            show.id,
            show.name,
            ambiguous,
        )
    return ShowMapResult(
        matched=matched,
        unmatched=unmatched,
        ambiguous=ambiguous,
        collisions=collisions,
        synthetic=synthetic,
    )


async def map_episode_ids(
    session: AsyncSession,
    client: TMDBClient,
    *,
    limit: int | None = None,
    batch_size: int = _BATCH_SIZE,
    failure_threshold: int = _FAILURE_THRESHOLD,
) -> EpisodeMapResult:
    """Map every matched show's copied episodes, committing as it goes.

    Owns its transaction boundaries for the reason `enrich_show_ids` does: this
    is hours of upstream calls, and work that cannot be reproduced for free has
    to be committed as it is done.

    A per-show failure is counted and stepped over — one broken series must not
    cost the pass — but `failure_threshold` consecutive real failures raise
    `EpisodeMapAborted`, because at that point the upstream is down rather than
    the data being odd. A 404 is neither: the show stays unmapped and the run
    carries on.

    `limit` caps how many shows are considered, which is how to try a hundred
    before committing to the full pass.
    """
    outcomes = ShowMapResult()
    total = (await session.execute(_REMAINING)).scalar_one()
    log.info(
        "episode map: %d matched show(s) have episodes left to map%s",
        total,
        f", considering {limit}" if limit else "",
    )

    after_id = 0
    considered = 0
    failed = 0
    gone = 0
    consecutive_failures = 0
    while limit is None or considered < limit:
        take = batch_size if limit is None else min(batch_size, limit - considered)
        rows = (await session.execute(_CANDIDATES, {"after_id": after_id, "limit": take})).all()
        if not rows:
            break

        for row in rows:
            show = ShowToMap(id=row.id, tmdb_id=row.tmdb_id, name=row.name)
            after_id = show.id
            considered += 1
            try:
                outcomes += await map_show_episodes(session, client, show)
            except Exception as exc:
                failed += 1
                if is_gone_upstream(exc):
                    gone += 1
                    log.info(
                        "show %d (%s): TMDB %d is gone upstream — left unmapped",
                        show.id,
                        show.name,
                        show.tmdb_id,
                    )
                    continue
                consecutive_failures += 1
                if isinstance(exc, httpx.HTTPStatusError):
                    # Retried to exhaustion by the client already, so this is a
                    # persistent upstream failure rather than a bug here — the
                    # status line is the whole story and a traceback is noise.
                    log.warning("show %d (%s): episode mapping failed: %s", show.id, show.name, exc)
                else:
                    # Anything else is ours, and the same distinction
                    # `mirror_series` draws: without the traceback a bug in this
                    # loop reads as an upstream problem.
                    log.exception(
                        "show %d (%s): unexpected episode mapping error", show.id, show.name
                    )
                if consecutive_failures >= failure_threshold:
                    # Committed first: the batch's earlier shows cost upstream
                    # calls and are correct, so an abort must not throw them away.
                    await session.commit()
                    raise EpisodeMapAborted(
                        f"aborted after {consecutive_failures} consecutive failures: {exc}"
                    ) from exc
                continue
            consecutive_failures = 0

        await session.commit()
        log.info(
            "episode map: %d/%d shows considered — %d episodes mapped, %d unmatched, "
            "%d ambiguous, %d collisions, %d synthetic specials, %d shows failed (%d gone)",
            considered,
            total,
            outcomes.matched,
            outcomes.unmatched,
            outcomes.ambiguous,
            outcomes.collisions,
            outcomes.synthetic,
            failed,
            gone,
        )

    return EpisodeMapResult(
        shows_considered=considered,
        shows_failed=failed,
        shows_gone=gone,
        episodes=outcomes,
    )


# --- the report -------------------------------------------------------------
#
# Read live rather than written to `docs/migration/`, for the same reason
# NEU-1044's queue is: a snapshot of it is stale the moment somebody re-runs the
# pass, and it names the rows a person would have to repair by hand.
#
# `app.user_episode_watch.episode_id` references `tvmaze.episode` until NEU-1046
# repoints it, and joining it to `catalog.episode.id` is nonetheless correct:
# the copy preserved TV Maze's ids as the catalog surrogates, which is the whole
# reason user data never has to move. The join stays correct past that point too.

# `synthetic` rides each row rather than only the totals: a watched null-numbered
# special (156 of them in production) is a permanent, understood residue, and a
# reader asked to tell it from a genuine mismatch by the sign of a number is
# being asked to know something the report should say.
_UNMATCHED_USER_DATA = text(f"""
    SELECT e.id AS episode_id,
           e.show_id,
           s.name AS show_name,
           s.tmdb_id AS show_tmdb_id,
           s.match_method,
           e.season_number,
           e.episode_number,
           e.name AS episode_name,
           e.air_date,
           e.episode_number < {_LOWEST_MAPPABLE_NUMBER} AS synthetic,
           (SELECT count(*) FROM app.user_episode_watch w WHERE w.episode_id = e.id) AS watches,
           (SELECT count(*) FROM app.user_episode_rating r WHERE r.episode_id = e.id) AS ratings
      FROM catalog.episode e
      JOIN catalog.show s ON s.id = e.show_id
     WHERE e.tmdb_id IS NULL
       AND (EXISTS (SELECT 1 FROM app.user_episode_watch w WHERE w.episode_id = e.id)
            OR EXISTS (SELECT 1 FROM app.user_episode_rating r WHERE r.episode_id = e.id))
     ORDER BY watches DESC, ratings DESC, e.show_id, e.season_number, e.episode_number
""")

# A show whose mappable episodes all failed, which is the ticket's distinct
# flag: scattered misses are episode-grain disagreements, none-of-them is a
# signal that the *show* is matched to the wrong series. Restricted to matched
# shows, since an unmatched show has nothing to have failed at.
_SYSTEMATIC_SHOWS = text(f"""
    SELECT s.id,
           s.name,
           s.tmdb_id,
           s.match_method,
           count(*) FILTER (WHERE e.episode_number >= {_LOWEST_MAPPABLE_NUMBER}) AS mappable,
           count(*) FILTER (WHERE e.episode_number < {_LOWEST_MAPPABLE_NUMBER}) AS synthetic,
           (SELECT count(*)
              FROM app.user_episode_watch w
              JOIN catalog.episode we ON we.id = w.episode_id
             WHERE we.show_id = s.id) AS watches
      FROM catalog.show s
      JOIN catalog.episode e ON e.show_id = s.id
     WHERE s.tmdb_id IS NOT NULL
     GROUP BY s.id, s.name, s.tmdb_id, s.match_method
    HAVING count(*) FILTER (WHERE e.episode_number >= {_LOWEST_MAPPABLE_NUMBER}) > 0
       AND count(*) FILTER (
               WHERE e.episode_number >= {_LOWEST_MAPPABLE_NUMBER} AND e.tmdb_id IS NOT NULL
           ) = 0
     ORDER BY watches DESC, s.id
""")

_TOTALS = text(f"""
    SELECT count(*) FILTER (WHERE e.tmdb_id IS NOT NULL) AS mapped,
           count(*) FILTER (
               WHERE e.tmdb_id IS NULL AND e.episode_number >= {_LOWEST_MAPPABLE_NUMBER}
           ) AS unmapped,
           count(*) FILTER (
               WHERE e.tmdb_id IS NULL AND e.episode_number < {_LOWEST_MAPPABLE_NUMBER}
           ) AS synthetic
      FROM catalog.episode e
      JOIN catalog.show s ON s.id = e.show_id
     WHERE s.tmdb_id IS NOT NULL
""")

# The acceptance criterion, as a query: every episode a user has watched is
# either mapped or in the report above.
_WATCHED_TOTALS = text("""
    SELECT count(*) AS watched_episodes,
           count(*) FILTER (WHERE e.tmdb_id IS NULL) AS watched_unmapped
      FROM catalog.episode e
     WHERE EXISTS (SELECT 1 FROM app.user_episode_watch w WHERE w.episode_id = e.id)
""")

# A watched episode with **no `catalog.episode` row at all** is invisible to every
# query above, which all read *from* that table — and it reads as a clean report,
# which is the one wrong answer this thing must not give. It happens for real, and
# nightly: the TV Maze daily keeps adding episodes right up to cutover, and every
# one added after the copy ran was watchable while having nothing to
# map. Same failure mode, same shape and same fix as
# `human_queue.unmirrored_user_touched_shows` — re-run the copy — so it is
# reported here rather than repaired.
_UNMIRRORED_WATCHES = text("""
    SELECT w.episode_id,
           count(*) AS watches
      FROM app.user_episode_watch w
     WHERE NOT EXISTS (SELECT 1 FROM catalog.episode e WHERE e.id = w.episode_id)
     GROUP BY w.episode_id
     ORDER BY watches DESC, w.episode_id
""")


@dataclass(frozen=True)
class EpisodeMapReport:
    """The whole report, JSON-shaped: it is read in a terminal over `ssh docker exec`."""

    totals: dict[str, int]
    unmatched_user_data: list[dict[str, Any]]
    systematic_shows: list[dict[str, Any]]
    # Watched episodes the copy never mirrored — not a mapping outcome at all.
    unmirrored_watches: list[dict[str, int]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "totals": self.totals,
            "unmatched_user_data": self.unmatched_user_data,
            "systematic_shows": self.systematic_shows,
            "unmirrored_watches": self.unmirrored_watches,
        }


async def build_report(session: AsyncSession) -> EpisodeMapReport:
    """Every unmapped episode a user has touched, worst first, plus the 0% shows.

    Needs no TMDB credential: it is entirely a read of what the pass left behind.

    **Read it after the pass, and read the pass's log beside it.** Before a run,
    every matched show has zero mapped episodes and so reads as systematic; and
    the systematic flag is `0 of N mapped`, which a show whose fetch failed
    during the run satisfies exactly as well as a show matched to the wrong
    series does. The database cannot tell those apart — the run's own per-show
    warnings are what does.
    """
    unmatched = [
        {
            "episode_id": row.episode_id,
            "show_id": row.show_id,
            "show_name": row.show_name,
            "show_tmdb_id": row.show_tmdb_id,
            "match_method": row.match_method,
            "season_number": row.season_number,
            "episode_number": row.episode_number,
            "episode_name": row.episode_name,
            "air_date": row.air_date.isoformat() if row.air_date else None,
            "synthetic": row.synthetic,
            "watches": row.watches,
            "ratings": row.ratings,
        }
        for row in (await session.execute(_UNMATCHED_USER_DATA)).all()
    ]
    systematic = [
        {
            "show_id": row.id,
            "name": row.name,
            "tmdb_id": row.tmdb_id,
            "match_method": row.match_method,
            "mappable_episodes": row.mappable,
            "synthetic_episodes": row.synthetic,
            "episode_watches": row.watches,
        }
        for row in (await session.execute(_SYSTEMATIC_SHOWS)).all()
    ]

    unmirrored = [
        {"episode_id": row.episode_id, "watches": row.watches}
        for row in (await session.execute(_UNMIRRORED_WATCHES)).all()
    ]

    totals = (await session.execute(_TOTALS)).one()
    watched = (await session.execute(_WATCHED_TOTALS)).one()
    return EpisodeMapReport(
        totals={
            "episodes_mapped": totals.mapped,
            "episodes_unmapped": totals.unmapped,
            "episodes_synthetic": totals.synthetic,
            "watched_episodes": watched.watched_episodes,
            "watched_episodes_unmapped": watched.watched_unmapped,
            "unmatched_carrying_user_data": len(unmatched),
            "systematic_shows": len(systematic),
            "unmirrored_watched_episodes": len(unmirrored),
        },
        unmatched_user_data=unmatched,
        systematic_shows=systematic,
        unmirrored_watches=unmirrored,
    )
