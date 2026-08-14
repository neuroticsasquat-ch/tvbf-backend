"""Stand one `app` -> `catalog` foreign key down for a block.

The module was named for a two-spine world: it also held `mirror_spine`, which
gave every seeded `tvmaze` row a `catalog` row of the same id, standing in for
NEU-1042's id-preserving copy. NEU-1051 dropped `tvmaze`, so tests seed
`catalog` directly and there is nothing left to mirror from. The name stays
because the concept it guards — which spine a user row resolves against — is
still what this file is about.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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

    Two reports exist to *find* rows this constraint makes impossible —
    `human_queue`'s `unmirrored_user_touched_shows` and `episode_map`'s
    `unmirrored_watches`. That was not redundant with the constraint: both ran
    **before** the repoint, on a database where the FK still pointed at `tvmaze`
    and a watch whose episode the copy never mirrored was an ordinary row. The
    state is unreachable now, so their tests have to reconstruct it, which since
    NEU-1046 means standing the constraint down first. (The coverage gate's
    `fk_targets_resolve` criterion asked the same question and went with the
    `tvmaze` schema in NEU-1051.)

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
