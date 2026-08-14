"""The airdate offset: what it means, how to read it, how to apply it (NEU-1145).

`catalog.air_date_offset` records how many days one season's mirrored airdates
are shifted from TMDB's own — see `AirDateOffset` in `catalog/models.py` for why
such a thing exists at all. This module is the one place that turns those rows
into corrected dates, and it has exactly two callers by design:

- **`tmdb/upsert.py` applies an offset as it writes.** Correcting on the way in
  is what keeps every reader right for free. A read-time correction would have
  to be threaded through browse, `/me/upcoming`, Watch Next, the season and
  episode pages and `episode_repo`'s `air_date <= today` aired filter — the
  many-call-sites asymmetry the specials ledger exists to police, where the site
  that forgets has no test.
- **`airdates/reconcile.py` projects an offset onto rows already stored**, once
  it has established one. Without that, a season would stay wrong until TMDB
  happened to change it and a delta re-fetched it, which for a finished season
  is never.

**Both go through `pair()` or `project_offsets`, and neither ever shifts a
stored value in place.** The corrected column and its `tmdb_*` twin are only
ever written together from the raw upstream value, which is held in
`tmdb_air_date` whenever a correction applies and in the visible column
otherwise. `corrected = raw + offset` is therefore an invariant rather than a
history of edits: re-running an ingest cannot double-apply a shift, changing an
offset is a local update rather than a re-fetch, and a genuine upstream ±1 day
change is picked up rather than mistaken for a correction already made. A design
that added the offset to whatever it found stored has no way to tell those two
apart.

Two rules about the show grain live here rather than in the writer, because
they are decisions and not plumbing (NEU-1145 §4.6): `first_air_date` takes
**season 1's** offset and `last_air_date` the offset of **the season holding the
last dated episode**. Ted Lasso is the proof that neither can be a blanket
per-show shift — seasons 1-2 were entered against the Eastern date and 3-4
against the Pacific one, so its true premiere must not move while its last-aired
date must.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from tvbf.catalog import models as m

log = logging.getLogger(__name__)

# The season whose offset `show.first_air_date` takes. A show premieres with its
# first numbered season, not with a special that may have aired around it, so
# season 0 is deliberately not a candidate.
FIRST_SEASON = 1


@dataclass(frozen=True)
class OffsetTable:
    """Every offset recorded for one show, and the override rule over them.

    Immutable and cheap: a show with no corrections at all — which is nearly
    every show — resolves to `EMPTY`, and `pair()` on it returns the upstream
    value untouched with a NULL twin.
    """

    by_season: Mapping[int | None, int]

    def for_season(self, season_number: int | None) -> int:
        """The offset that applies to one season. 0 when none does.

        A numbered row wins over the show-wide default, which is the whole of
        the override rule. An unknown season number falls through to the
        default, so an operator can correct a show whose seasons are still
        arriving without listing them.
        """
        if season_number is not None:
            specific = self.by_season.get(season_number)
            if specific is not None:
                return specific
        return self.by_season.get(None, 0)

    def pair(
        self, value: date | None, season_number: int | None
    ) -> tuple[date | None, date | None]:
        """`(corrected, raw)` for one upstream value — the pair every writer sets.

        `raw` is `None` when no correction applies, which is what makes NULL in
        a `tmdb_*` column mean "this row is untouched TMDB" rather than "nobody
        recorded the original".
        """
        if value is None:
            return None, None
        days = self.for_season(season_number)
        if not days:
            return value, None
        return value + timedelta(days=days), value

    def __bool__(self) -> bool:
        return bool(self.by_season)


EMPTY = OffsetTable({})


def season_of_last_dated(dated_seasons: Iterable[tuple[int, date | None]]) -> int | None:
    """Which season holds the latest dated episode, given `(season_number, date)` pairs.

    The rule `show.last_air_date` takes its offset from. A tie goes to the
    higher season number, which is the one a viewer would call current.

    Pure, and fed by both callers: the ingest passes the payload's episodes,
    the projection passes the mirrored rows. Neither can substitute its own
    idea of "the last season" — a show whose most recent season has no dates
    yet has not aired from it.
    """
    best: tuple[date, int] | None = None
    for season_number, value in dated_seasons:
        if value is None:
            continue
        candidate = (value, season_number)
        if best is None or candidate > best:
            best = candidate
    return None if best is None else best[1]


async def _rows(session: AsyncSession, *, where: ColumnElement[bool]) -> OffsetTable:
    result = await session.execute(
        select(m.AirDateOffset.season_number, m.AirDateOffset.offset_days).where(where)
    )
    return OffsetTable({r.season_number: r.offset_days for r in result.all()})


async def load_offsets(session: AsyncSession, *, show_id: int) -> OffsetTable:
    """Every offset recorded for one show, by surrogate id."""
    return await _rows(session, where=m.AirDateOffset.show_id == show_id)


async def load_offsets_by_tmdb_id(session: AsyncSession, *, tmdb_id: int | None) -> OffsetTable:
    """Every offset recorded for one show, by TMDB id.

    The ingest's entry point, and the reason it exists: `upsert_show` writes
    `first_air_date` and mints the surrogate id in the same statement, so the
    offsets have to be in hand before the id they are keyed on is known. A show
    the mirror has never seen has no offsets by construction, so the join
    finding nothing is the ordinary case rather than a miss.
    """
    if tmdb_id is None:
        return EMPTY
    return await _rows(
        session,
        where=m.AirDateOffset.show_id.in_(select(m.Show.id).where(m.Show.tmdb_id == tmdb_id)),
    )


def _group_by_offset(offsets: OffsetTable, season_numbers: Iterable[int]) -> dict[int, list[int]]:
    """`{offset_days: [season numbers]}` over the seasons actually present.

    Resolving each season through `for_season` first is what lets one code path
    handle a numbered row, the show-wide default and the absence of both: a
    season resolving to 0 lands in the group whose statement *retracts* a
    correction, which is how a deleted offset row un-does itself.
    """
    groups: dict[int, list[int]] = {}
    for season_number in season_numbers:
        groups.setdefault(offsets.for_season(season_number), []).append(season_number)
    return groups


async def _project_column(
    session: AsyncSession,
    *,
    table: type[m.Season] | type[m.Episode],
    corrected: InstrumentedAttribute[date | None],
    raw: InstrumentedAttribute[date | None],
    show_id: int,
    scope: ColumnElement[bool],
    days: int,
) -> int:
    """Re-derive one date column from its raw twin over a scoped set of rows.

    Idempotent by construction, because the value written is a function of the
    raw column and the offset alone — never of what is currently displayed. The
    `IS DISTINCT FROM` guard is therefore only about the returned count, so a
    nightly re-run reports the rows it changed rather than the rows it touched.
    """
    upstream = func.coalesce(raw, corrected)
    if days:
        values: dict[InstrumentedAttribute[date | None], object] = {
            corrected: upstream + timedelta(days=days),
            raw: upstream,
        }
        changed = corrected.is_distinct_from(upstream + timedelta(days=days))
    else:
        # Retraction: no offset applies any more, so the stored value goes back
        # to upstream's and the twin goes back to NULL.
        values = {corrected: raw, raw: None}
        changed = raw.is_not(None)
    result = await session.execute(
        update(table)
        .where(table.show_id == show_id, scope, upstream.is_not(None), changed)
        .values(values)
    )
    return result.rowcount  # type: ignore[attr-defined]


async def project_offsets(session: AsyncSession, *, show_id: int) -> int:
    """Re-derive every stored date on one show from its raw value and its offsets.

    Returns the number of rows changed. Run after writing offsets — a correction
    that only took effect the next time TMDB happened to change the show would
    never reach a finished season.

    This is **not** the "re-apply the offset after the delta" design the pairing
    above rejects. Nothing here reads a corrected value: every write is
    `raw + offset`, with `raw` taken from the `tmdb_*` twin when one is set and
    from the visible column when it is not, so running it twice, running it
    after an ingest already applied the same offset, and running it after the
    offset changed all converge on the same rows.

    The caller owns the transaction.
    """
    offsets = await load_offsets(session, show_id=show_id)

    changed = 0

    episode_seasons = (
        (
            await session.execute(
                select(m.Episode.season_number).where(m.Episode.show_id == show_id).distinct()
            )
        )
        .scalars()
        .all()
    )
    for days, numbers in _group_by_offset(offsets, episode_seasons).items():
        changed += await _project_column(
            session,
            table=m.Episode,
            corrected=m.Episode.air_date,
            raw=m.Episode.tmdb_air_date,
            show_id=show_id,
            scope=m.Episode.season_number.in_(numbers),
            days=days,
        )

    season_numbers = (
        (
            await session.execute(
                select(m.Season.season_number).where(m.Season.show_id == show_id).distinct()
            )
        )
        .scalars()
        .all()
    )
    for days, numbers in _group_by_offset(offsets, season_numbers).items():
        changed += await _project_column(
            session,
            table=m.Season,
            corrected=m.Season.air_date,
            raw=m.Season.tmdb_air_date,
            show_id=show_id,
            scope=m.Season.season_number.in_(numbers),
            days=days,
        )

    changed += await _project_show_dates(session, show_id=show_id, offsets=offsets)
    return changed


async def _project_show_dates(session: AsyncSession, *, show_id: int, offsets: OffsetTable) -> int:
    """The two show-grain dates, each keyed to the season it derives from.

    `last_air_date`'s season is read back off the mirrored episodes using their
    **raw** dates, so the answer does not move as the correction is applied.
    """
    dated = (
        await session.execute(
            select(
                m.Episode.season_number,
                func.max(func.coalesce(m.Episode.tmdb_air_date, m.Episode.air_date)),
            )
            .where(m.Episode.show_id == show_id)
            .group_by(m.Episode.season_number)
        )
    ).all()
    last_season = season_of_last_dated((r[0], r[1]) for r in dated)

    changed = 0
    for corrected, raw, season_number in (
        (m.Show.first_air_date, m.Show.tmdb_first_air_date, FIRST_SEASON),
        (m.Show.last_air_date, m.Show.tmdb_last_air_date, last_season),
    ):
        days = offsets.for_season(season_number)
        upstream = func.coalesce(raw, corrected)
        if days:
            values: dict[InstrumentedAttribute[date | None], object] = {
                corrected: upstream + timedelta(days=days),
                raw: upstream,
            }
            guard = corrected.is_distinct_from(upstream + timedelta(days=days))
        else:
            values = {corrected: raw, raw: None}
            guard = raw.is_not(None)
        result = await session.execute(
            update(m.Show).where(m.Show.id == show_id, upstream.is_not(None), guard).values(values)
        )
        changed += result.rowcount  # type: ignore[attr-defined]
    return changed


async def replace_season_offsets(
    session: AsyncSession,
    *,
    show_id: int,
    verdicts: Sequence[tuple[int, int | None, int]],
) -> tuple[int, int]:
    """Record the reconciliation job's verdicts. Returns `(written, retracted)`.

    Each verdict is `(season_number, offset_days, episodes_compared)` with
    `offset_days` of 0 meaning *the oracle and TMDB agree about this season*,
    which retracts any offset previously recorded for it.

    **A season the job refused is simply absent from `verdicts`, and is left
    alone.** Refusing is the absence of a verdict, not a verdict of zero: the
    trust rule declines whenever the evidence is ambiguous, and treating that as
    "no shift" would let one oddly-numbered season undo a correction established
    from clean evidence the night before.

    Show-wide rows (`season_number IS NULL`) are never written and never
    retracted here — they are the operator's, and this pass does not overwrite a
    human's decision with an inference.
    """
    written = 0
    retracted = 0
    for season_number, offset_days, episodes_compared in verdicts:
        if offset_days:
            result = await session.execute(
                _upsert_offset(
                    show_id=show_id,
                    season_number=season_number,
                    offset_days=offset_days,
                    episodes_compared=episodes_compared,
                )
            )
            written += result.rowcount  # type: ignore[attr-defined]
        else:
            result = await session.execute(
                _delete_offset(show_id=show_id, season_number=season_number)
            )
            retracted += result.rowcount  # type: ignore[attr-defined]
    return written, retracted


def _upsert_offset(*, show_id: int, season_number: int, offset_days: int, episodes_compared: int):
    values = {
        "show_id": show_id,
        "season_number": season_number,
        "offset_days": offset_days,
        "episodes_compared": episodes_compared,
    }
    return (
        insert(m.AirDateOffset)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_air_date_offset_show_season",
            set_={
                "offset_days": offset_days,
                "episodes_compared": episodes_compared,
                "updated_at": func.now(),
            },
        )
    )


def _delete_offset(*, show_id: int, season_number: int):
    return delete(m.AirDateOffset).where(
        m.AirDateOffset.show_id == show_id,
        m.AirDateOffset.season_number == season_number,
    )
