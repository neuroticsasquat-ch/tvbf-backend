"""Snapshot every watch and rating record into `app.watch_archive` (NEU-1029).

The TMDB migration's no-loss guarantee is "no user loses a tracked show, a
watched episode, or a rating, period." This module is what makes that absolute
rather than aspirational: it copies all four source tables into a table that
describes what was watched in human terms, so a catastrophic mapping failure
stays recoverable by hand even after `tvmaze` is gone.

It now reads `catalog`, which is what "after `tvmaze` is gone" stopped being
hypothetical about in NEU-1051. The archive's own shape is unchanged and its
existing rows are still correct: NEU-1042 preserved TV Maze's ids as the catalog
surrogates, so `source_show_id` and `source_episode_id` name the same rows they
always did, and `app` has referenced `catalog` since NEU-1046 anyway — a run
that read the old spine would have been describing a row the user's history no
longer points at.

Three properties are worth stating outright, because each is a decision:

* **One statement per record type.** Each snapshot is a single
  `INSERT ... SELECT ... ON CONFLICT DO NOTHING` executed inside Postgres. No
  row-by-row Python loop, so the archive is a consistent read of the source
  tables rather than a smear across however long the job takes.
* **`DO NOTHING`, never `DO UPDATE`.** Re-running is idempotent *and*
  append-only — a second run adds rows for source rows that appeared since, and
  cannot rewrite what the first run recorded. If a user unwatches and re-watches
  an episode, the archive keeps the original snapshot, which is the point. The
  `watch_archive_no_mutation` trigger enforces the same thing at the table.
* **The run verifies itself, by anti-join.** Insert counts prove nothing on a
  re-run (they are legitimately zero), and neither does `archived >= source`:
  once a source row is deleted after being archived, the leftover archive row
  covers for a genuinely missing one. So the check counts *source rows with no
  archive row* and raises `ArchiveIncomplete` on any. That is the ticket's
  acceptance criterion — one archive row per source row — stated literally.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import (
    BigInteger,
    Date,
    Integer,
    Numeric,
    Select,
    Text,
    cast,
    exists,
    extract,
    func,
    literal,
    null,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import DomainError
from tvbf.app.models import (
    User,
    UserEpisodeRating,
    UserEpisodeWatch,
    UserShowRating,
    UserShowWatch,
    WatchArchive,
    watch_archive_record_type_enum,
)
from tvbf.catalog.models import Episode, Show

log = logging.getLogger(__name__)

# The order the `INSERT ... SELECT` columns are bound in. Every `_select_*`
# helper below builds its SELECT in exactly this order.
_ARCHIVE_COLUMNS = (
    "record_type",
    "user_id",
    "user_email",
    "user_display_name",
    "show_name",
    "show_premiered_year",
    "season_number",
    "episode_number",
    "episode_title",
    "episode_airdate",
    "occurred_at",
    "stars",
    "source_show_id",
    "source_episode_id",
    "show_imdb_id",
    "show_tvdb_id",
)


class ArchiveIncomplete(DomainError):
    """Some source row has no archive row. Raised by the self-verification pass.

    Deliberately fatal: an archive that silently covers only part of the history
    is worse than none, because it would be trusted.
    """

    def __init__(self, unarchived: dict[str, int]) -> None:
        detail = ", ".join(
            f"{record_type}: {count} unarchived"
            for record_type, count in sorted(unarchived.items())
        )
        super().__init__(f"watch archive is incomplete ({detail})")
        self.unarchived = unarchived


@dataclass(frozen=True, slots=True)
class RecordTypeCounts:
    """One record type's outcome.

    `inserted` is what *this* run added, legitimately 0 on a re-run.
    `unarchived` is the number that decides the run: source rows with no
    matching archive row. It has to be 0, and — unlike comparing the two
    totals — it stays honest when the archive holds rows whose source row has
    since been deleted, which inflates `archived` above `source`.
    """

    source: int
    inserted: int
    archived: int
    unarchived: int


@dataclass(frozen=True, slots=True)
class ArchiveSnapshot:
    """Per-record-type counts for one snapshot run."""

    counts: dict[str, RecordTypeCounts]

    @property
    def source_total(self) -> int:
        return sum(c.source for c in self.counts.values())

    @property
    def inserted_total(self) -> int:
        return sum(c.inserted for c in self.counts.values())

    @property
    def archived_total(self) -> int:
        return sum(c.archived for c in self.counts.values())


def _premiered_year():
    """`Show.first_air_date`'s year as an integer, NULL when there is no premiere date.

    `extract` yields a numeric in Postgres; the cast keeps the archive column an
    honest `integer` rather than storing `2009.0`.
    """
    return cast(extract("year", Show.first_air_date), Integer)


def _null(type_):
    """A NULL of an explicit type.

    A bare NULL in a subquery's select list resolves to `text` in Postgres, so
    `_unarchived_count` would compare `bigint = text` and fail outright. Typing
    the placeholder keeps the SELECT usable both as an INSERT source and as a
    subquery.
    """
    return cast(null(), type_)


def _archive_select(*columns) -> Select:
    """A SELECT whose columns are labelled with the archive column names.

    The labels are not cosmetic: `_unarchived_count` turns these SELECTs into
    subqueries and joins them back against the archive by name, so the positional
    agreement with `_ARCHIVE_COLUMNS` is checked here (`strict=True`) rather than
    trusted four times over.
    """
    return select(*[c.label(name) for c, name in zip(columns, _ARCHIVE_COLUMNS, strict=True)])


def _record_type(value: str):
    """The record type as a literal of the enum's own type.

    Cast explicitly rather than left as an untyped bind parameter: the target is
    an enum column and the driver should not have to infer that.
    """
    return cast(literal(value), watch_archive_record_type_enum)


def _select_show_watches() -> Select:
    return (
        _archive_select(
            _record_type("show_watch"),
            UserShowWatch.user_id,
            User.email,
            User.display_name,
            Show.name,
            _premiered_year(),
            _null(Integer),
            _null(Integer),
            _null(Text),
            _null(Date),
            UserShowWatch.created_at,
            _null(Numeric(2, 1)),
            Show.id,
            _null(BigInteger),
            Show.imdb_id,
            Show.tvdb_id,
        )
        .join(User, User.id == UserShowWatch.user_id)
        .join(Show, Show.id == UserShowWatch.show_id)
    )


def _select_show_ratings() -> Select:
    return (
        _archive_select(
            _record_type("show_rating"),
            UserShowRating.user_id,
            User.email,
            User.display_name,
            Show.name,
            _premiered_year(),
            _null(Integer),
            _null(Integer),
            _null(Text),
            _null(Date),
            UserShowRating.rated_at,
            UserShowRating.stars,
            Show.id,
            _null(BigInteger),
            Show.imdb_id,
            Show.tvdb_id,
        )
        .join(User, User.id == UserShowRating.user_id)
        .join(Show, Show.id == UserShowRating.show_id)
    )


def _select_episode_watches() -> Select:
    return (
        _archive_select(
            _record_type("episode_watch"),
            UserEpisodeWatch.user_id,
            User.email,
            User.display_name,
            Show.name,
            _premiered_year(),
            Episode.season_number,
            Episode.episode_number,
            Episode.name,
            Episode.air_date,
            UserEpisodeWatch.watched_at,
            _null(Numeric(2, 1)),
            Show.id,
            Episode.id,
            Show.imdb_id,
            Show.tvdb_id,
        )
        .join(User, User.id == UserEpisodeWatch.user_id)
        .join(Episode, Episode.id == UserEpisodeWatch.episode_id)
        .join(Show, Show.id == Episode.show_id)
    )


def _select_episode_ratings() -> Select:
    return (
        _archive_select(
            _record_type("episode_rating"),
            UserEpisodeRating.user_id,
            User.email,
            User.display_name,
            Show.name,
            _premiered_year(),
            Episode.season_number,
            Episode.episode_number,
            Episode.name,
            Episode.air_date,
            UserEpisodeRating.rated_at,
            UserEpisodeRating.stars,
            Show.id,
            Episode.id,
            Show.imdb_id,
            Show.tvdb_id,
        )
        .join(User, User.id == UserEpisodeRating.user_id)
        .join(Episode, Episode.id == UserEpisodeRating.episode_id)
        .join(Show, Show.id == Episode.show_id)
    )


# Record type -> (its SELECT builder, the source table to count).
_SOURCES = {
    "show_watch": (_select_show_watches, UserShowWatch),
    "episode_watch": (_select_episode_watches, UserEpisodeWatch),
    "show_rating": (_select_show_ratings, UserShowRating),
    "episode_rating": (_select_episode_ratings, UserEpisodeRating),
}


async def _count(db: AsyncSession, table: type) -> int:
    return (await db.execute(select(func.count()).select_from(table))).scalar_one()


async def _archived_count(db: AsyncSession, record_type: str) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(WatchArchive)
            .where(WatchArchive.record_type == record_type)
        )
    ).scalar_one()


async def _unarchived_count(db: AsyncSession, build_select) -> int:
    """Source rows of this record type with no archive row — the acceptance test.

    An anti-join rather than `archived < source`, because the two totals stop
    being comparable the moment a source row is deleted after being archived:
    the leftover archive row then covers for a genuinely missing one. Matched on
    the same four columns as `uq_watch_archive_source_row`, with
    `IS NOT DISTINCT FROM` on the nullable one so show-grain rows match.
    """
    src = build_select().subquery()
    matching = select(literal(1)).where(
        WatchArchive.record_type == src.c.record_type,
        WatchArchive.user_id == src.c.user_id,
        WatchArchive.source_show_id == src.c.source_show_id,
        WatchArchive.source_episode_id.is_not_distinct_from(src.c.source_episode_id),
    )
    return (
        await db.execute(select(func.count()).select_from(src).where(~exists(matching)))
    ).scalar_one()


async def snapshot(db: AsyncSession) -> ArchiveSnapshot:
    """Archive every watch and rating record, then verify the archive is complete.

    Commits once, after all four inserts, so a failure part-way leaves the
    archive exactly as the previous run left it. Raises `ArchiveIncomplete` if
    any source row ended up without an archive row.
    """
    counts: dict[str, RecordTypeCounts] = {}

    for record_type, (build_select, source_table) in _SOURCES.items():
        source = await _count(db, source_table)
        result = await db.execute(
            pg_insert(WatchArchive)
            .from_select(list(_ARCHIVE_COLUMNS), build_select())
            .on_conflict_do_nothing(constraint="uq_watch_archive_source_row")
        )
        inserted = result.rowcount  # type: ignore[attr-defined]
        counts[record_type] = RecordTypeCounts(
            source=source,
            inserted=inserted,
            archived=await _archived_count(db, record_type),
            unarchived=await _unarchived_count(db, build_select),
        )
        log.info(
            "watch archive %s: %d source, %d inserted, %d archived, %d unarchived",
            record_type,
            source,
            inserted,
            counts[record_type].archived,
            counts[record_type].unarchived,
        )

    unarchived = {record_type: c.unarchived for record_type, c in counts.items() if c.unarchived}
    if unarchived:
        await db.rollback()
        raise ArchiveIncomplete(unarchived)

    await db.commit()
    return ArchiveSnapshot(counts=counts)
