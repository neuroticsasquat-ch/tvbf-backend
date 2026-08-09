"""Copy `tvmaze.*` into `catalog.*` with the ids preserved (NEU-1042).

**This is the whole migration strategy in one file.** Surrogate keys are opaque
(ADR-0008), so `catalog.show.id` can simply *be* the old `tvmaze.show.id` — and
once it is, `app.user_show_watch.show_id` and `app.user_episode_watch.episode_id`
never change. The user-data migration stops being a rewrite and becomes
re-pointing a constraint (NEU-1046); every `/shows/:id` URL in the wild keeps
working; and an episode that never matches TMDB is already a valid
`catalog.episode` row with `tmdb_id IS NULL`, so the no-loss guarantee is
structural rather than procedural.

It lives under `tvmaze/` for the same reason `tmdb/upsert.py` lives under
`tmdb/`: the mapping out of a source's shape belongs to that source. It is also
transitional by construction and retires with the schema it reads (NEU-1050).

**Every row lands with `tmdb_id IS NULL`.** Enrichment is NEU-1043's, and the
null is not a placeholder — it is the sanctioned marker for a row no upstream
owns, so a show TMDB never matches simply stays that way forever.

**Ordering matters, and not in the direction the milestone numbers suggest.**
This has to run *before* the full TMDB ingest (NEU-1034), not after. The ingest
mints a fresh surrogate for every series it inserts; run it first and a show
present in both sources ends up as two `catalog.show` rows, whereupon NEU-1043
violates `uq_show_tmdb_id` stamping a `tmdb_id` the other row already holds. In
this order no special handling is needed anywhere: the NEU-1033 upsert
conflict-targets `tmdb_id`, so an enriched row is found and updated in place
with its preserved id intact.

Three things about the copy itself are worth knowing before editing it.

**What is *not* copied is deliberate.** Genres, networks and web channels are
left behind. `catalog.genre` and `catalog.network` are keyed on `tmdb_id`, so a
copied genre row (`tmdb_id IS NULL`) would never match the one the ingest
creates and the tables would carry every genre twice. They cost one request to
re-derive and nothing references them from `app`.

**Columns are copied only where the value is still a correct instance of what
the column means.** `summary` is an overview, so it travels. A TV Maze image is
a full URL where `poster_path` holds a TMDB path fragment, and `language` is
`"English"` where `original_language` holds `"en"` — those are left null for
NEU-1043 and NEU-1034 to fill, rather than stored as values whose consumers
would misread them.

`status` and `type` are the two knowing exceptions, and both carry a foreign
vocabulary until enrichment overwrites them. `status` because `is_ended` is
generated from it, and a null status reads as still-running — wrong for the ~76%
of the mirror that has ended. `type` because 15,024 rows hold values TMDB never
emits (`Animation`, `Variety`, `Game Show`, …) and dropping them would leave the
browse type filter with nothing to say about a show TMDB never matches. Both
columns therefore hold two vocabularies at once between this job and the ingest;
the browse filters that read them are NEU-1047's to reshape.

**Every insert is `ON CONFLICT (id) DO NOTHING`.** That is what makes the job
re-runnable, and it is also what stops a re-run undoing enrichment — a row the
TMDB pass has since updated is left exactly as it is.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# Episodes are copied a block of shows at a time rather than in one statement.
#
# The unit is **shows, not episodes**, and that is load-bearing: the synthetic
# episode numbering below is a window function over `(show_id, season)`, so
# splitting one show across two batches would restart its numbering and produce
# different numbers than a single-batch run.
#
# What batching buys is progress and bounded per-statement work, not a smaller
# transaction — the caller still commits once, and at 3.5M rows the whole copy
# measures 44 seconds. Progress that only appeared at the end would be
# indistinguishable from a hang.
_SHOW_BATCH_SIZE = 5_000


@dataclass(frozen=True)
class TableCopy:
    table: str
    source_rows: int
    copied_rows: int
    # Source ids with no row on the other side, found by anti-join rather than
    # by comparing the two counts. The ticket asks for matching counts, but
    # counts are the weaker check and this repo has already learnt why once
    # (`watch_archive_service`): the moment the destination holds a row the
    # source does not — which is precisely what the TMDB ingest will do to
    # `catalog.show` — the totals stop being comparable, and before that a
    # deleted source row would let a genuinely missing one hide behind a
    # balanced total.
    missing_rows: int

    @property
    def complete(self) -> bool:
        return self.missing_rows == 0


@dataclass(frozen=True)
class CopyResult:
    tables: list[TableCopy]
    sequences: dict[str, int]

    @property
    def complete(self) -> bool:
        return all(t.complete for t in self.tables)


_COPY_SHOWS = text("""
    INSERT INTO catalog.show (
        id, name, status, type, overview, homepage,
        first_air_date, last_air_date, runtime, vote_average,
        imdb_id, tvdb_id, tvrage_id, deleted_upstream_at, ingested_at
    )
    SELECT
        s.id, s.name, s.status, s.type, s.summary, s.official_site,
        s.premiered, s.ended, s.runtime, s.rating_average,
        s.externals_imdb, s.externals_tvdb, s.externals_tvrage,
        s.deleted_upstream_at, s.ingested_at
    FROM tvmaze.show s
    ON CONFLICT (id) DO NOTHING
""")

# `name` -> `title` is the only rename that matters: `CONTEXT.md` fixes "AKA" as
# the word for this concept, so the column is `title` and the table is
# `show_aka` rather than TMDB's `alternative_titles`.
#
# `country_name` and `language` are dropped. `catalog.show_aka` carries a country
# code and a free-text `type`, and neither of the two has a home — the country
# name is derivable from the code, and TMDB states no language on an alternative
# title.
_COPY_SHOW_AKAS = text("""
    INSERT INTO catalog.show_aka (id, show_id, title, country_code)
    SELECT a.id, a.show_id, a.name, a.country_code
    FROM tvmaze.show_aka a
    ON CONFLICT (id) DO NOTHING
""")

_COPY_SEASONS = text("""
    INSERT INTO catalog.season (
        id, show_id, season_number, name, overview, air_date, episode_count
    )
    SELECT s.id, s.show_id, s.number, s.name, s.summary, s.premiere_date, s.episode_order
    FROM tvmaze.season s
    ON CONFLICT (id) DO NOTHING
""")

# The one mapping that is not a rename.
#
# `catalog.episode.episode_number` is NOT NULL, because under TMDB a special is
# season 0 with a real episode number (audit D2). `tvmaze.episode.number` is
# nullable, because under TV Maze a null number *was* the marker for a special —
# **27,498 episodes in prod, 156 of them watched by a real user.** They cannot be
# dropped, and they cannot be moved to season 0 either: 27,458 of them carry a
# real season number, which season 0 would discard.
#
# So the season number is preserved and the episode number is synthesised
# **negative**: -1, -2, … within the season. Two reasons, and the first is a bug
# the obvious scheme has.
#
# *Numbering them contiguously after the season's highest real number collides.*
# `tvmaze` keeps moving under this job — the daily update writes it until
# NEU-1050 — and this job is advertised re-runnable. Give a special number 24 in
# one run, let the daily add a genuine episode 24, and the next run copies that
# real episode onto a number a special already holds. Nothing catches it:
# `catalog.episode` has no unique key on `(show_id, season_number,
# episode_number)`, and `ON CONFLICT (id) DO NOTHING` leaves the earlier row
# alone. Real episode numbers start at 1, so a negative can never be collided
# with.
#
# *And a contiguous number lies.* "S3E24" reads as the season's 24th episode.
# A negative says, unmistakably, that we made it up — which for a row that will
# carry `tmdb_id IS NULL` forever is the honest signal. They sort ahead of the
# premiere, which is what TMDB's own season-0 specials do anyway (audit §7).
#
# The floor is the most negative number **already assigned in `catalog`** for
# that season, not just what this batch can see, so a re-run appends rather than
# restarting the count — a special the daily adds later takes the next ordinal
# down whatever its upstream id happens to be, and no existing row moves.
#
# These rows keep `tmdb_id IS NULL` permanently. The audit already says why — a
# null-numbered special has no `(season_number, episode_number)` counterpart
# upstream to match on — so they are exactly the residue NEU-1045 reports.
_COPY_EPISODES = text("""
    WITH season_floor AS (
        SELECT
            e.show_id,
            e.season,
            least(
                coalesce(
                    (SELECT min(ce.episode_number) FROM catalog.episode ce
                     WHERE ce.show_id = e.show_id AND ce.season_number = e.season),
                    0
                ),
                0
            ) AS floor_number
        FROM tvmaze.episode e
        WHERE e.show_id >= :first_show_id AND e.show_id < :next_show_id
        GROUP BY e.show_id, e.season
    )
    INSERT INTO catalog.episode (
        id, show_id, season_id, season_number, episode_number,
        name, overview, air_date, runtime, vote_average
    )
    SELECT
        e.id, e.show_id, e.season_id, e.season,
        coalesce(
            e.number,
            sf.floor_number - row_number() OVER (
                PARTITION BY e.show_id, e.season, (e.number IS NULL)
                ORDER BY e.id
            )
        ),
        e.name, e.summary, e.airdate, e.runtime, e.rating_average
    FROM tvmaze.episode e
    JOIN season_floor sf ON sf.show_id = e.show_id AND sf.season = e.season
    WHERE e.show_id >= :first_show_id AND e.show_id < :next_show_id
      -- Rows already copied are excluded rather than left to ON CONFLICT, so
      -- the ordinal counts only what is genuinely new: without it a re-run
      -- numbers past the specials it already placed and leaves gaps. The
      -- conflict clause stays as the backstop it always was.
      AND NOT EXISTS (SELECT 1 FROM catalog.episode ce WHERE ce.id = e.id)
    ON CONFLICT (id) DO NOTHING
""")

# Source table -> destination table, in the order the FKs require.
_TABLES: tuple[tuple[str, str], ...] = (
    ("tvmaze.show", "catalog.show"),
    ("tvmaze.show_aka", "catalog.show_aka"),
    ("tvmaze.season", "catalog.season"),
    ("tvmaze.episode", "catalog.episode"),
)

# Identity columns that have to clear the copied ids afterwards, and the start
# NEU-1032 configured for each. `show_aka` is in here and is the one that would
# actually have bitten: its identity starts at 1, well inside the 85,707 ids the
# copy brings across, so the next generated row would collide immediately. The
# three spine tables start at 1,000,000 / 1,000,000 / 10,000,000 and already
# clear prod's maxima (93,485 / 204,059 / 3,695,163) — this re-asserts that
# rather than trusting it.
_IDENTITIES: tuple[tuple[str, int], ...] = (
    ("catalog.show", 1_000_000),
    ("catalog.season", 1_000_000),
    ("catalog.episode", 10_000_000),
    ("catalog.show_aka", 1),
)


async def _count(session: AsyncSession, table: str) -> int:
    return (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()


async def _count_missing(session: AsyncSession, source: str, destination: str) -> int:
    """Source rows with no row of the same id on the other side."""
    return (
        await session.execute(
            text(
                f"SELECT count(*) FROM {source} s "
                f"WHERE NOT EXISTS (SELECT 1 FROM {destination} d WHERE d.id = s.id)"
            )
        )
    ).scalar_one()


async def _copy_episodes(session: AsyncSession) -> None:
    """Copy episodes a block of shows at a time, logging as it goes."""
    bounds = (await session.execute(text("SELECT min(id), max(id) FROM tvmaze.show"))).one()
    lowest, highest = bounds
    if lowest is None:
        return
    copied = 0
    for start in range(lowest, highest + 1, _SHOW_BATCH_SIZE):
        result = await session.execute(
            _COPY_EPISODES,
            {"first_show_id": start, "next_show_id": start + _SHOW_BATCH_SIZE},
        )
        copied += result.rowcount  # type: ignore[attr-defined]
        log.info(
            "episodes: shows %d-%d done, %d rows copied so far",
            start,
            min(start + _SHOW_BATCH_SIZE, highest + 1) - 1,
            copied,
        )


async def _align_identity(session: AsyncSession, table: str, start: int) -> int:
    """Restart the table's identity clear of every copied id.

    Without this the next generated id lands on a row the copy already put
    there. `RESTART WITH` takes a literal rather than an expression, hence the
    read-then-write.

    The counter never moves backwards — the restart point is the highest of the
    copied maximum, the start NEU-1032 configured, and wherever the sequence has
    already reached. Re-running after the TMDB ingest has minted ids of its own
    must not rewind into territory that has already been handed out.
    """
    highest = (
        await session.execute(text(f"SELECT coalesce(max(id), 0) FROM {table}"))
    ).scalar_one()
    # The sequence name comes out of the catalog, so interpolating it is safe in
    # the way `Bucket.table` is: it is never caller-supplied.
    sequence = (
        await session.execute(text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": table})
    ).scalar_one()
    last_value, is_called = (
        await session.execute(text(f"SELECT last_value, is_called FROM {sequence}"))
    ).one()
    next_free = last_value + 1 if is_called else last_value

    restart_at = max(highest + 1, start, next_free)
    await session.execute(text(f"ALTER TABLE {table} ALTER COLUMN id RESTART WITH {restart_at}"))
    return restart_at


async def verify_copy(session: AsyncSession) -> list[TableCopy]:
    """Report, per table, how much of the source has a row on the other side.

    Separate from the copy so the state can be checked without writing — during
    a cutover window "is this already done?" is a question worth being able to
    ask cheaply.
    """
    return [
        TableCopy(
            table=destination,
            source_rows=await _count(session, source),
            copied_rows=await _count(session, destination),
            missing_rows=await _count_missing(session, source, destination),
        )
        for source, destination in _TABLES
    ]


async def copy_to_catalog(session: AsyncSession) -> CopyResult:
    """Run the whole copy and report what landed. The caller owns the transaction.

    Idempotent: every statement is `ON CONFLICT (id) DO NOTHING`, so a re-run
    fills only what is missing and rewrites nothing.
    """
    log.info("copying shows")
    await session.execute(_COPY_SHOWS)
    log.info("copying show AKAs")
    await session.execute(_COPY_SHOW_AKAS)
    log.info("copying seasons")
    await session.execute(_COPY_SEASONS)
    log.info("copying episodes")
    await _copy_episodes(session)

    tables = await verify_copy(session)
    sequences = {
        table: await _align_identity(session, table, start) for table, start in _IDENTITIES
    }
    return CopyResult(tables=tables, sequences=sequences)
