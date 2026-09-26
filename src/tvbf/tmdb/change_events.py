"""Catalog change detection: what the delta saw change on a tracked show (NEU-1481).

Project spec §5.1, ADR-0014. The delta's per-show transaction reads a
`ShowSnapshot` before `upsert_series_payload` overwrites the rows, compares it
with the payload that is about to land, and writes one `catalog.show_event` per
transition through `catalog/events.py` — so the events land or roll back with
the upsert that revealed them.

Split in three so the rules are testable without a database: `snapshot_tracked_show`
is the read, `detect_transitions` the pure comparison, `record_transitions` the
write. `tmdb/ingest.py:mirror_series` owns *when* it runs — only on a
`catalog_update` run, never the full pass or a backfill.

Two rules are load-bearing:

- **The comparison reads TMDB's raw dates, never corrected ones.** A season's
  old date is `coalesce(tmdb_air_date, air_date)` — the raw value when an
  airdate offset applies, the stored one when none does — and the new date is
  the payload's own `air_date`. Comparing against the corrected `air_date`
  would turn every offset season into a one-day "move" on each re-fetch.
- **A date's first appearance is only news if it is still ahead.** A premiere
  date that arrives already past is TMDB backfilling history, and a move with
  both ends in the past is a correction; neither is an announcement.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app import models as am
from tvbf.catalog import models as m
from tvbf.catalog.events import ShowEventKind, record_events
from tvbf.tmdb.api_payloads import TMDBSeries

# The vocabulary of the generated `catalog.show.is_ended` column.
TERMINAL_STATUSES = frozenset({"Ended", "Canceled"})


@dataclass(frozen=True)
class ShowSnapshot:
    """A tracked show as stored, just before the delta overwrites it."""

    status: str | None
    # season_number -> TMDB's raw premiere date. A number missing here means the
    # season had no row; one mapped to `None` had a row with no date.
    premieres: Mapping[int, date | None]


@dataclass(frozen=True)
class Transition:
    """One change worth a `catalog.show_event` row, before it has surrogate ids."""

    kind: ShowEventKind
    old_value: str | None
    new_value: str | None
    # Set for the two premiere kinds; resolved to `season_id` after the upsert.
    season_number: int | None = None


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _status_transition(old: str | None, new: str | None) -> Transition | None:
    was_over = old in TERMINAL_STATUSES
    if not was_over and new in TERMINAL_STATUSES:
        return Transition("ended", old, new)
    # A status going null is TMDB losing the field, not the show coming back.
    if was_over and new is not None and new not in TERMINAL_STATUSES:
        return Transition("revived", old, new)
    return None


def _premiere_transition(
    season_number: int, old: date | None, new: date | None, *, today: date
) -> Transition | None:
    if new is None:
        return None
    if old is None:
        if new > today:
            return Transition("premiere_set", None, _iso(new), season_number)
        return None
    if new != old and (new > today or old > today):
        return Transition("premiere_moved", _iso(old), _iso(new), season_number)
    return None


def detect_transitions(old: ShowSnapshot, new: TMDBSeries, *, today: date) -> list[Transition]:
    """Every transition between a stored snapshot and the payload replacing it.

    Pure, so each kind and each non-event is unit-testable. Status first, then
    premieres in season order — a stable order, since delivery reads events by
    `id` within a transaction.
    """
    transitions: list[Transition] = []
    status = _status_transition(old.status, new.status)
    if status is not None:
        transitions.append(status)

    # Season 0 is specials: a date there is not a premiere anyone waits for.
    incoming = {s.season_number: s.air_date for s in new.seasons if s.season_number > 0}
    for number in sorted(incoming):
        premiere = _premiere_transition(
            number, old.premieres.get(number), incoming[number], today=today
        )
        if premiere is not None:
            transitions.append(premiere)
    return transitions


async def snapshot_tracked_show(session: AsyncSession, *, tmdb_id: int) -> ShowSnapshot | None:
    """The stored state of a series about to be upserted, or `None` to skip it.

    `None` when no user tracks the show (ADR-0014 §2) — which includes a series
    the catalog has not mirrored yet, since nobody can track a row that does not
    exist.
    """
    tracked = exists().where(am.UserShowWatch.show_id == m.Show.id)
    show = (
        await session.execute(
            select(m.Show.id, m.Show.status).where(m.Show.tmdb_id == tmdb_id, tracked)
        )
    ).first()
    if show is None:
        return None

    seasons = await session.execute(
        select(m.Season.season_number, func.coalesce(m.Season.tmdb_air_date, m.Season.air_date))
        .where(m.Season.show_id == show.id)
        # Descending, so a duplicated number keeps its lowest id — the same row
        # `record_transitions` resolves to, or the comparison could flap.
        .order_by(m.Season.id.desc())
    )
    return ShowSnapshot(status=show.status, premieres=dict(seasons.tuples().all()))


async def record_transitions(
    session: AsyncSession,
    *,
    show_id: int,
    run_id: UUID,
    transitions: Sequence[Transition],
) -> None:
    """Write `transitions` as `catalog.show_event` rows in the caller's transaction.

    Runs **after** the upsert, because a premiere's season may be one the upsert
    just created: its surrogate id is looked up by `(show_id, season_number)`.
    `catalog.season` has no uniqueness on that pair, so a duplicated number
    resolves to its lowest id.
    """
    if not transitions:
        return
    numbers = {t.season_number for t in transitions if t.season_number is not None}
    season_ids: dict[int, int] = {}
    if numbers:
        rows = await session.execute(
            select(m.Season.season_number, m.Season.id)
            .where(m.Season.show_id == show_id, m.Season.season_number.in_(numbers))
            .order_by(m.Season.id.desc())
        )
        # Descending, so the lowest id is the one left standing in the dict.
        season_ids = dict(rows.tuples().all())

    await record_events(
        session,
        [
            m.ShowEvent(
                kind=t.kind,
                show_id=show_id,
                season_id=season_ids.get(t.season_number) if t.season_number is not None else None,
                old_value=t.old_value,
                new_value=t.new_value,
                run_id=run_id,
            )
            for t in transitions
        ],
    )
