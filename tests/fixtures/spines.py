"""Give the seeded `tvmaze` rows a `catalog` row of the same id.

Since NEU-1046 the four `app.user_*` foreign keys reference `catalog.show` and
`catalog.episode`, so a test that seeds a show into `tvmaze` and then tracks it
as a user has seeded only half of what the constraint needs. In production the
other half is NEU-1042's id-preserving copy, which ran once over the whole
mirror; here it is this helper, and the premise is identical — `catalog.show.id`
for a migrated show simply *is* its old `tvmaze.show.id` (ADR-0008).

It is deliberately **not** autouse. A handful of tests exist precisely to prove
what happens to a row the copy never mirrored (`human_queue`'s
`unmirrored_user_touched_shows`, `episode_map`'s `unmirrored_watches`), and an
automatic mirror would quietly delete those tests' subject matter. Call it where
a test needs the premise; leave it out where the absence is the point.

It is also not `tvmaze.catalog_copy.copy_to_catalog`, though it means the same
thing. That job walks the show-id space in blocks of 5,000 to keep the real
3.5M-episode copy off one statement, and test ids sit up around 9,400,000 — so
reusing it would spend ~1,900 queries per call to move four rows.

Only shows and episodes are mirrored, because those are the only two tables
`app` references. Seasons are left out along with `catalog.episode.season_id`,
which is nullable by design.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_MIRROR_SHOWS = text("""
    INSERT INTO catalog.show (id, name, status, type, first_air_date, last_air_date)
    SELECT s.id, s.name, s.status, s.type, s.premiered, s.ended
    FROM tvmaze.show s
    ON CONFLICT (id) DO NOTHING
""")

# `catalog.episode.episode_number` is NOT NULL where `tvmaze.episode.number` is
# nullable for a special, so a null is synthesised negative — the same signal
# the production copy uses to say the value was invented, minus its
# within-season ordinal, which nothing here reads.
_MIRROR_EPISODES = text("""
    INSERT INTO catalog.episode (
        id, show_id, season_number, episode_number, name, air_date, runtime
    )
    SELECT e.id, e.show_id, e.season, coalesce(e.number, -e.id), e.name, e.airdate, e.runtime
    FROM tvmaze.episode e
    WHERE EXISTS (SELECT 1 FROM catalog.show cs WHERE cs.id = e.show_id)
    ON CONFLICT (id) DO NOTHING
""")


async def mirror_spine(session: AsyncSession) -> None:
    """Mirror every `tvmaze` show and episode into `catalog`, id preserved.

    Idempotent, so it is safe to call after each seeding step and safe on a test
    that has already written its own `catalog` rows — those are left exactly as
    they are, which is what `ON CONFLICT (id) DO NOTHING` buys the real copy too.
    """
    await session.execute(_MIRROR_SHOWS)
    await session.execute(_MIRROR_EPISODES)
    await session.flush()


# The four `app` -> `catalog` foreign keys, by the table they sit on. Only the
# constraint name and the column are named here: the *definition* is read back
# out of Postgres below rather than restated, so this cannot drift from what
# `app/models.py` declares.
# The catalog table each referencing column resolves against, for the anti-join
# the exit uses to clear the rows the restored constraint would reject.
_TARGETS: dict[str, str] = {"show_id": "show", "episode_id": "episode"}

_APP_FKS: dict[str, tuple[str, str]] = {
    "user_show_watch": ("fk_usw_show", "show_id"),
    "user_show_rating": ("fk_user_show_rating_show", "show_id"),
    "user_episode_watch": ("fk_uew_episode", "episode_id"),
    "user_episode_rating": ("fk_user_episode_rating_episode", "episode_id"),
}


@asynccontextmanager
async def without_catalog_fk(session: AsyncSession, table: str) -> AsyncIterator[None]:
    """Drop one `app` -> `catalog` foreign key for the duration of the block.

    Several passes exist to *find* rows this constraint makes impossible — the
    coverage gate's `fk_targets_resolve` criterion, `human_queue`'s
    `unmirrored_user_touched_shows`, `episode_map`'s `unmirrored_watches`. That
    is not redundant with the constraint: every one of them runs **before** the
    repoint, on a database where the FK still points at `tvmaze` and a watch
    whose episode the copy never mirrored is an ordinary row. Their tests have
    to reconstruct that state, which since NEU-1046 means standing the
    constraint down first.

    The definition is **snapshotted with `pg_get_constraintdef` and replayed**,
    never retyped. `scripts/refresh_db.sh` does the same thing for the same
    reason, and `CLAUDE.md` says why: replaying a hardcoded list is exactly how
    the ratings and `import_ne` constraints got silently dropped. Here the
    failure would be quieter still — a helper that restored `ON DELETE CASCADE`
    onto a constraint someone had since changed would leave every later test in
    the session asserting against a constraint the models no longer declare, and
    all of them would pass.

    Restoring needs the impossible rows gone, so the exit deletes exactly the
    ones that do not resolve — never a row that does, so anything the test
    seeded legitimately survives to be asserted on afterwards.
    """
    constraint, column = _APP_FKS[table]
    definition = (
        await session.execute(
            text(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "WHERE c.conname = :constraint "
                "AND c.conrelid = (:qualified)::regclass"
            ),
            {"constraint": constraint, "qualified": f"app.{table}"},
        )
    ).scalar_one()
    await session.execute(text(f"ALTER TABLE app.{table} DROP CONSTRAINT {constraint}"))
    try:
        yield
    finally:
        await session.execute(
            text(
                f"DELETE FROM app.{table} src WHERE NOT EXISTS "
                f"(SELECT 1 FROM catalog.{_TARGETS[column]} dst WHERE dst.id = src.{column})"
            )
        )
        await session.execute(
            text(f"ALTER TABLE app.{table} ADD CONSTRAINT {constraint} {definition}")
        )
        await session.commit()
