"""Establish a per-season airdate offset against the TV Maze oracle (NEU-1145).

The pass that makes the correction automatic. It compares every mirrored show a
user tracks — or that has an episode still to air — against TV Maze, and records
one integer per season where the evidence is unanimous. The only other thing
kept is the show's id on the oracle, cached by `airdates/show_state` so the
lookup is not re-derived nightly (NEU-1148 §7); nothing else about TV Maze is
stored.

**The trust rule is the whole design, and it refuses rather than guesses.** An
offset is written for a `(show_id, season_number)` only when all three hold:

1. every episode carrying a date on both sides, whose `(season_number,
   episode_number)` pairing is unambiguous on both sides, differs by the **same**
   amount;
2. that amount is exactly **±1**;
3. at least **two** such episodes exist.

The clamp is principled rather than defensive: the disagreement we are
authorised to correct is timezone-shaped, and a timezone artifact can only ever
produce ±1 day. Anything else is a different disagreement, and following it
would make the mirror track the oracle's schedule instead of fixing TMDB's
coast. Shrinking S3 is the worked example — TMDB spreads a two-episode premiere
across two weeks, so the per-episode deltas run `+1, +6, +6, …` and both the
clamp and the unanimity rule reject it independently, where a job trusting the
oracle's delta would have moved nine episodes by six days. The cost is real and
accepted: that season stays a day early, and appears in the refusal log where a
human can find it. A per-episode correction would fix it and is rejected — a
season that contradicts itself in a list is a worse artifact than one that is
uniformly wrong, and it would look deliberate rather than broken.

**The comparison is always against the raw upstream date**, never the corrected
one — `coalesce(tmdb_air_date, air_date)`. Reading the visible column would make
last night's correction this night's evidence of agreement, and the offset would
retract itself on the second run.

**The pass is network-agnostic.** Choosing an oracle removed the need for the
weekday heuristic the ticket was written around, so there is no network
allowlist and no Apple/Prime asymmetry anywhere in this module. A per-network or
weekday-derived rule was measured and rejected: for Apple TV+ "Tue/Thu ⇒
Pacific" is right on 197 of 200 rows, and the same rule corrupts 17
currently-correct Prime Video rows to fix 93, because Prime really does release
on those days.

**Scope and due are two different questions** (NEU-1149). A show is *in scope* —
ours to correct at all — if a user tracks it or it holds a future-dated episode.
It is *due* on a given night only if something could have changed for it: it has
never been reconciled, TMDB touched it since we last looked, it is still airing,
or its sweep turn has come. The pass originally re-derived every offset every
night, which made its cost proportional to catalog size plus user count when the
thing needing re-checking is proportional to change — and the population that
grows with the user base, tracked shows that have finished airing, is exactly
where nightly re-checking buys nothing.

**The sweep is what preserves the self-healing property.** The first three
clauses of `shows_to_check` see every change on TMDB's side, because
`catalog.show.tmdb_synced_at` advances precisely when the delta re-mirrored a
show. None of them can see a change on the
**oracle's** side — TV Maze correcting a date on a finished show TMDB never
touches — and without a backstop an offset that should be retracted never would
be. So every show is re-checked on a `SWEEP_DAYS` cadence regardless of whether
anything flagged it, which is the property re-checking everything nightly used to
give for free. The sweep is amortised rather than periodic-in-bulk: a show's
bucket is `show_id % SWEEP_DAYS`, so one seventh of the scope is swept each night
and no night ever spikes. A plain "last reconciled more than a week ago" would
instead synchronise permanently on the first run after deploy — six quiet nights
and one that reconciles the entire scope, forever — which reimports the scaling
problem one night in seven.

**Widening the list to the whole catalog is still refused.** The 115,731 shows
carrying an external id remain a one-line change here and deliberately not made:
~32 hours of sustained traffic against a free, keyless, unfunded API for a
five-user app is poor etiquette, and being blocked would take the fix down with
it.
"""

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import BigInteger, and_, cast, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from tvbf.airdates.api_payloads import TVMazeEpisode
from tvbf.airdates.client import TVMazeOracleClient
from tvbf.airdates.show_state import mark_reconciled, oracle_episodes
from tvbf.app import models as am
from tvbf.catalog import models as m
from tvbf.catalog.offsets import project_offsets, replace_season_offsets
from tvbf.catalog.runs import finalize_run, record_progress
from tvbf.config import Settings
from tvbf.db import SessionLocal

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]

# The only correction this pass may write, in either direction. See the module
# docstring — and `ck_air_date_offset_days`, which says the same thing at the
# table so a hand-entered row cannot bypass it either.
MAX_OFFSET_DAYS = 1

# The trust rule's third clause. Two agreeing episodes is a season; one is a
# coincidence, and every off-by-one in the data would satisfy a rule of one.
MIN_EPISODES = 2

# How often a show is re-checked even though nothing flagged it as changed. The
# backstop for drift on the oracle's side, which no watermark of ours can see —
# see the module docstring for why the pass cannot do without one.
#
# A module constant beside `MAX_OFFSET_DAYS` and `MIN_EPISODES`, which are the
# precedent NEU-1148 §4 set: rules about what the pass believes, changed by a
# code change with a test rather than by an operator.
SWEEP_DAYS = 7

# How far behind its sweep turn a show may fall before it is due regardless of
# bucket. The bucket rule alone costs a missed night — container down, or a run
# that aborted before reaching those shows — a full extra interval. At 1× this
# would fire for the whole scope one week after a cold start, reintroducing
# exactly the synchronisation the bucketing removes; at 2× it fires only for
# shows that are genuinely overdue, which is when a spike is wanted.
_SWEEP_FLOOR = timedelta(days=2 * SWEEP_DAYS)

_PROGRESS_EVERY = 50


@dataclass(frozen=True)
class SeasonVerdict:
    """What the trust rule concluded about one season.

    `offset_days` is `None` for a refusal, which is the absence of a verdict
    rather than a verdict of zero: an offset established last night from clean
    evidence must survive tonight's ambiguous evidence, so a refusal leaves any
    existing row alone. Zero is a real verdict — *the two agree* — and retracts.
    """

    season_number: int
    offset_days: int | None
    episodes_compared: int
    reason: str | None = None
    deltas: tuple[int, ...] = ()


@dataclass
class ReconcileResult:
    shows_considered: int = 0
    shows_without_external_id: int = 0
    shows_not_found: int = 0
    shows_failed: int = 0
    # Seasons the oracle could not speak to at all, counted apart from a refusal
    # because there was no evidence to reject — see `judge_seasons`.
    seasons_uncomparable: int = 0
    offsets_written: int = 0
    offsets_retracted: int = 0
    seasons_refused: int = 0
    rows_corrected: int = 0
    # What `airdates/show_state` spent or avoided (NEU-1148 §9). Flat fields
    # rather than a nested object because they are read by the closing log
    # line, and `lookups_spent` falling to near zero across consecutive runs is
    # the acceptance criterion — observable in production, not only in a test.
    lookups_spent: int = 0
    links_reused: int = 0
    links_invalidated: int = 0
    # Why tonight's shows are due, one counter per clause of `shows_to_check`
    # (NEU-1149 §7). They overlap — an airing show TMDB also touched counts in
    # two — because the question they answer is "which clause is growing", and
    # forcing them to partition would answer it only for whichever clause was
    # tested first. Without them a 1,790-show night reads identically whether it
    # is the airing population or a sweep clause misfiring.
    due_never: int = 0
    due_changed: int = 0
    due_airing: int = 0
    due_swept: int = 0
    aborted: bool = False
    refusals: list[tuple[int, SeasonVerdict]] = field(default_factory=list)


@dataclass(frozen=True)
class DueReasons:
    """Which of `shows_to_check`'s clauses put this show on tonight's list.

    Carried on the show rather than summed in SQL so the run can report the
    breakdown without a second round trip, and so a test can assert that a show
    is due *for the reason it was seeded to be due for* rather than merely
    present — which is the whole of NEU-1149's acceptance criteria 2 to 5.
    """

    never: bool
    changed: bool
    airing: bool
    swept: bool


@dataclass(frozen=True)
class ShowToCheck:
    show_id: int
    name: str
    imdb_id: str | None
    tvdb_id: int | None
    due: DueReasons


def _episode_key(season: int | None, number: int | None) -> tuple[int, int] | None:
    """`(season, episode)` as a key, or `None` when the pair cannot key anything.

    TV Maze numbers a special `null`, and the migration's copied specials carry
    a negative number (see `catalog/episodes.py`). Neither can be paired with
    the other side, so both drop out here rather than being coerced into a key
    that would pair the wrong episodes.
    """
    if season is None or number is None or number < 0:
        return None
    return season, number


def _unambiguous(
    entries: Iterable[tuple[tuple[int, int], date]],
) -> dict[tuple[int, int], date]:
    """`{(season, episode): date}`, dropping every key that appears twice.

    Uniqueness is required **on both sides**, which is why this runs over the
    oracle's episodes and over ours alike. TV Maze's copy left 2,298 duplicate
    `(show, season, number)` triples in the mirror and 13 shows number two
    seasons the same; a duplicated key cannot say which of two dates it means,
    and picking one would be the pass adjudicating a question it was not asked.
    """
    seen: dict[tuple[int, int], date] = {}
    duplicated: set[tuple[int, int]] = set()
    for key, value in entries:
        if key in seen:
            duplicated.add(key)
            continue
        seen[key] = value
    for key in duplicated:
        del seen[key]
    return seen


def judge_seasons(
    ours: dict[tuple[int, int], date], theirs: dict[tuple[int, int], date]
) -> list[SeasonVerdict]:
    """Apply the trust rule to one show, a season at a time.

    Both maps are already `_unambiguous`, and both hold the **raw** upstream
    date for their side. Seasons the two do not share simply produce no verdict.
    """
    by_season: dict[int, list[int]] = defaultdict(list)
    for key, value in ours.items():
        other = theirs.get(key)
        if other is not None:
            # Days to ADD to TMDB's value to reach the oracle's — the direction
            # `offset_days` is defined in, and the direction `OffsetTable.pair`
            # applies it in.
            by_season[key[0]].append((other - value).days)

    # Seasons we hold that the oracle could not speak to at all — most often
    # season 0, whose specials neither side can key. They are verdicts rather
    # than an omission so nothing about the pass is silent, and they are
    # separated from a refusal because there was no evidence to reject.
    for season_number, _ in ours:
        by_season.setdefault(season_number, [])

    verdicts: list[SeasonVerdict] = []
    for season_number, deltas in sorted(by_season.items()):
        compared = len(deltas)
        distinct = set(deltas)
        if compared == 0:
            verdicts.append(SeasonVerdict(season_number, None, 0, "no_overlap"))
        elif compared < MIN_EPISODES:
            verdicts.append(
                SeasonVerdict(season_number, None, compared, "too_few", tuple(sorted(distinct)))
            )
        elif len(distinct) > 1:
            verdicts.append(
                SeasonVerdict(
                    season_number, None, compared, "inconsistent", tuple(sorted(distinct))
                )
            )
        elif abs(deltas[0]) > MAX_OFFSET_DAYS:
            verdicts.append(
                SeasonVerdict(
                    season_number, None, compared, "out_of_range", tuple(sorted(distinct))
                )
            )
        else:
            verdicts.append(SeasonVerdict(season_number, deltas[0], compared))
    return verdicts


def _todays_bucket() -> ColumnElement[int]:
    """Tonight's sweep bucket, off the database's own clock.

    Days since the epoch modulo `SWEEP_DAYS`, rather than a day-of-week
    comparison — which reads more directly and silently pins the interval to 7,
    so `SWEEP_DAYS` would stop being a real constant the moment anybody changed
    it.
    """
    days = cast(func.extract("epoch", func.current_date()) / 86400, BigInteger)
    return days % SWEEP_DAYS


async def shows_to_check(
    session: AsyncSession, *, today_bucket: int | None = None
) -> list[ShowToCheck]:
    """The shows in scope that are due tonight, and why each one is.

    **Scope** is unchanged: every show a user tracks, or that holds an episode
    still to air. The tracked half is what makes the correction reach the shows
    somebody would notice; the future-dated half is what makes a newly tracked
    show already correct when somebody adds it. Measured in production on
    2026-08-14, the two are nearly *disjoint* rather than nearly coincident —
    560 tracked, 1,787 with a future episode, 27 in both — which is the claim
    NEU-1145 §4.4 got wrong and NEU-1149 §1.1 corrects. The scope predicate lives
    here and nowhere else, which is what makes widening it the one-line change
    NEU-1145 §9 says it is.

    **Due** is ANDed onto that scope, never substituted for it. A show is due
    when any of:

    1. it has never been reconciled — a new show, or one somebody just tracked;
    2. `catalog.show.tmdb_synced_at` has advanced since, which happens precisely
       when the delta re-mirrored the show because TMDB reported a change;
    3. it is still airing, so its dates can still move;
    4. its sweep turn has come, or it has fallen `2 * SWEEP_DAYS` behind one.

    `last_reconciled_at IS NULL` alone would match all ~229k mirrored shows,
    which is the ~32-hour run NEU-1145 §9 refuses; the scope is what bounds it.

    **Clause 3 readmits every airing show every night**, including the ~1,760
    nobody tracks, so the list is not proportional to change — it is proportional
    to what is currently airing, plus what changed. That is deliberate: an airing
    season's evidence changes on the *oracle's* side by nature, as TV Maze adds
    episodes that have only just been announced, and that is exactly what turns a
    season refused for `too_few` into one that can finally reach a verdict. TMDB
    may not change at all in that window, having entered the full season already,
    so no watermark of ours would fire until the sweep. What the predicate does
    remove is the population that grows with the user base: shows a user tracks
    that have finished airing, whose dates will never change again.

    `today_bucket` overrides the database's clock so the sweep's tests assert
    something that does not change with the calendar — the same shape
    `oracle_episodes`' `relookup_missing_after` takes, and for the same reason.
    """
    tracked = exists().where(am.UserShowWatch.show_id == m.Show.id)
    airing = exists().where(
        m.Episode.show_id == m.Show.id,
        func.coalesce(m.Episode.tmdb_air_date, m.Episode.air_date) > func.current_date(),
    )
    last = m.AirdateShowState.last_reconciled_at

    never = last.is_(None)
    # NULL on either side yields NULL, which an OR reads as not-true and a CASE
    # reads as its ELSE — so a never-reconciled show is counted by `never` alone
    # rather than by both clauses.
    changed = m.Show.tmdb_synced_at > last
    bucket = _todays_bucket() if today_bucket is None else literal(today_bucket % SWEEP_DAYS)
    swept = and_(
        last.is_not(None),
        or_(m.Show.id % SWEEP_DAYS == bucket, last < func.now() - _SWEEP_FLOOR),
    )

    rows = (
        await session.execute(
            select(
                m.Show.id,
                m.Show.name,
                m.Show.imdb_id,
                m.Show.tvdb_id,
                never.label("never"),
                func.coalesce(changed, False).label("changed"),
                airing.label("airing"),
                func.coalesce(swept, False).label("swept"),
            )
            # Outer, because "no row" is the never-reconciled state clause 1
            # reads — an inner join would silently drop every show the pass has
            # not yet met, which is every show on the first run after deploy.
            .outerjoin(m.AirdateShowState, m.AirdateShowState.show_id == m.Show.id)
            .where(or_(tracked, airing), or_(never, changed, airing, swept))
            .order_by(m.Show.id)
        )
    ).all()
    return [
        ShowToCheck(
            r.id,
            r.name,
            r.imdb_id,
            r.tvdb_id,
            DueReasons(never=r.never, changed=r.changed, airing=r.airing, swept=r.swept),
        )
        for r in rows
    ]


async def _our_episodes(session: AsyncSession, *, show_id: int) -> dict[tuple[int, int], date]:
    """Our raw upstream dates for one show, keyed and de-duplicated.

    `coalesce(tmdb_air_date, air_date)` is the raw TMDB value in both states —
    the twin when a correction has been applied, the visible column when it has
    not. Comparing the corrected value instead would make last night's
    correction tonight's evidence that the two sides agree.
    """
    rows = (
        await session.execute(
            select(
                m.Episode.season_number,
                m.Episode.episode_number,
                func.coalesce(m.Episode.tmdb_air_date, m.Episode.air_date),
            ).where(m.Episode.show_id == show_id)
        )
    ).all()
    entries = []
    for season_number, episode_number, value in rows:
        key = _episode_key(season_number, episode_number)
        if key is not None and value is not None:
            entries.append((key, value))
    return _unambiguous(entries)


def _oracle_episodes(payload: Sequence[TVMazeEpisode]) -> dict[tuple[int, int], date]:
    """The oracle's side of the comparison, keyed and de-duplicated.

    Parsing happened at the client boundary, so an unscheduled episode's `""`
    `airdate` is already `None` here — the `OptionalDate` coercion every
    upstream date field in this repo goes through, rather than a guard restated
    at the point of use.
    """
    entries = [
        (key, episode.airdate)
        for episode in payload
        if (key := _episode_key(episode.season, episode.number)) is not None
        and episode.airdate is not None
    ]
    return _unambiguous(entries)


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory):
    session = session_factory()
    try:
        yield session
    finally:
        await session.close()


async def _reconcile_show(
    session: AsyncSession,
    client: TVMazeOracleClient,
    show: ShowToCheck,
    result: ReconcileResult,
) -> None:
    """One show, in its own transaction. Raises only on a genuine failure."""
    # One call, which spends a lookup only when there is no usable cached id —
    # and which owns the stale-link retry. `None` is "TV Maze has no
    # counterpart", however we learned it: the log below is identical whether
    # the answer came from a request or from a row, because its subject is
    # which shows cannot be corrected and not how we found out.
    episodes = await oracle_episodes(session, client, show, result)
    if episodes is None:
        result.shows_not_found += 1
        log.info("show %d (%s): no TV Maze counterpart", show.show_id, show.name)
        return

    ours = await _our_episodes(session, show_id=show.show_id)
    theirs = _oracle_episodes(episodes)

    verdicts = judge_seasons(ours, theirs)
    decided = [
        (v.season_number, v.offset_days, v.episodes_compared)
        for v in verdicts
        if v.offset_days is not None
    ]
    for verdict in verdicts:
        if verdict.offset_days is not None:
            continue
        if verdict.reason == "no_overlap":
            result.seasons_uncomparable += 1
            log.debug(
                "show %d (%s) season %d: nothing comparable upstream",
                show.show_id,
                show.name,
                verdict.season_number,
            )
            continue
        # The refusal log AC 5 asks for: the season, why, and the per-episode
        # deltas that were rejected, so a consistently-wrong season is findable
        # by a human rather than merely absent.
        result.seasons_refused += 1
        result.refusals.append((show.show_id, verdict))
        log.info(
            "show %d (%s) season %d: no offset written (%s) over %d episode(s), deltas %s",
            show.show_id,
            show.name,
            verdict.season_number,
            verdict.reason,
            verdict.episodes_compared,
            list(verdict.deltas),
        )

    written, retracted = await replace_season_offsets(
        session, show_id=show.show_id, verdicts=decided
    )
    result.offsets_written += written
    result.offsets_retracted += retracted
    # Always, not only when an offset changed: the ingest may have written rows
    # since the last pass, and projecting is idempotent by construction.
    result.rows_corrected += await project_offsets(session, show_id=show.show_id)


async def run_airdate_reconcile(
    *,
    session_factory: SessionFactory,
    client: TVMazeOracleClient,
    run_id: UUID,
    failure_threshold: int = 10,
) -> ReconcileResult:
    """One full pass. Per-show failures are counted, not fatal.

    Same failure contract as every other pass over the catalog: a show that
    raises is logged and counted, and only `failure_threshold` **consecutive**
    failures abort — which is how a genuinely broken upstream is told apart from
    a broken show. A show TV Maze has never heard of is not a failure at all;
    the client answers `None` rather than raising for exactly that reason.
    """
    result = ReconcileResult()

    async with _owned_session(session_factory) as s:
        shows = await shows_to_check(s)

    without_ids = [s for s in shows if s.imdb_id is None and s.tvdb_id is None]
    result.shows_without_external_id = len(without_ids)
    if without_ids:
        # No silent caps: these are in scope and cannot be checked, so they are
        # named rather than quietly dropped from the denominator.
        log.warning(
            "%d show(s) in scope carry no imdb_id or tvdb_id and cannot be looked up: %s",
            len(without_ids),
            ", ".join(f"{s.show_id} ({s.name})" for s in without_ids[:20]),
        )
    checkable = [s for s in shows if s.imdb_id is not None or s.tvdb_id is not None]

    for show in checkable:
        result.due_never += show.due.never
        result.due_changed += show.due.changed
        result.due_airing += show.due.airing
        result.due_swept += show.due.swept

    log.info(
        "airdate reconciliation: %d show(s) due — %d airing, %d changed, "
        "%d never reconciled, %d swept",
        len(checkable),
        result.due_airing,
        result.due_changed,
        result.due_never,
        result.due_swept,
    )

    consecutive_failures = 0
    for index, show in enumerate(checkable, start=1):
        try:
            async with _owned_session(session_factory) as s:
                await _reconcile_show(s, client, show, result)
                # Same session and transaction as the show's own work, and
                # immediately before its commit: a crash between the two would
                # otherwise record a show as done that was not. Not inside
                # `_reconcile_show`, which returns early on "no counterpart" and
                # would need the stamp on both paths — and that early return is
                # a genuine conclusion worth stamping, or the ~500-show negative
                # population stays in every night's work list forever.
                await mark_reconciled(s, show.show_id)
                await s.commit()
        except Exception:
            log.exception("show %d (%s): airdate reconciliation failed", show.show_id, show.name)
            result.shows_failed += 1
            consecutive_failures += 1
            # Recorded at the failure site, one at a time, exactly as
            # `tmdb/ingest.py` does. `record_progress` *increments* the column,
            # so handing it the running total on the batch tick below would
            # re-add every earlier failure on every tick.
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                result.aborted = True
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s,
                        run_id,
                        status="failed",
                        error=f"{consecutive_failures} consecutive show failures",
                    )
                    await s.commit()
                return result
        else:
            consecutive_failures = 0
        finally:
            result.shows_considered += 1

        if index % _PROGRESS_EVERY == 0:
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, processed_delta=_PROGRESS_EVERY)
                await s.commit()

    async with _owned_session(session_factory) as s:
        await record_progress(s, run_id, processed_delta=result.shows_considered % _PROGRESS_EVERY)
        await finalize_run(s, run_id, status="succeeded")
        await s.commit()

    log.info(
        "airdate reconciliation: %d show(s) considered, %d not on TV Maze, %d failed; "
        "%d offset(s) written, %d retracted, %d season(s) refused, %d with nothing comparable, "
        "%d row(s) corrected; %d lookup(s) spent, %d link(s) reused, %d invalidated",
        result.shows_considered,
        result.shows_not_found,
        result.shows_failed,
        result.offsets_written,
        result.offsets_retracted,
        result.seasons_refused,
        result.seasons_uncomparable,
        result.rows_corrected,
        result.lookups_spent,
        result.links_reused,
        result.links_invalidated,
    )
    return result


def _session_factory() -> AsyncSession:
    return SessionLocal()


async def run_airdate_reconcile_job(run_id: UUID, settings: Settings) -> None:
    """One pass, wired from settings and guaranteed to finalize.

    The shape `run_catalog_update_job` already has, and for the same reason: the
    scheduled entrypoint awaits this, and anything that escapes it would leave a
    `running` row for the stale-run cleanup to find hours later.
    """
    try:
        async with TVMazeOracleClient(
            base_url=settings.tvmaze_base_url,
            rate_calls=settings.tvmaze_rate_limit_requests,
            rate_window=settings.tvmaze_rate_limit_window_seconds,
            retry_max_attempts=settings.tvmaze_retry_max_attempts,
        ) as client:
            await run_airdate_reconcile(
                session_factory=_session_factory,
                client=client,
                run_id=run_id,
                failure_threshold=settings.ingest_consecutive_failure_threshold,
            )
    except Exception as e:
        log.exception("airdate reconciliation crashed")
        async with SessionLocal() as s:
            await finalize_run(s, run_id, status="failed", error=str(e))
            await s.commit()
