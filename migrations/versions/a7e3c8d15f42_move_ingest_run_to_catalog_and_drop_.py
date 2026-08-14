"""Move ingest_run into catalog, then drop the tvmaze schema

Revision ID: a7e3c8d15f42
Revises: c9a3f1e60b47
Create Date: 2026-08-13

The end of the TMDB migration (NEU-1051). Two statements, and the order between
them is the whole migration: `ingest_run` is the one table in `tvmaze` that live
code still reads, so it moves out before the schema goes.

**`SET SCHEMA`, not create-and-copy.** The table carries production run rows
going back to the first TV Maze ingest — including the `catalog_initial` and
`catalog_update` rows the delta's cursor bootstraps off (`get_completed_pass_start`
reads the earliest attempt's `started_at`). `SET SCHEMA` moves the table with its
data, its primary key, its check constraints and its indexes in one statement;
recreating it and copying would have to restate all of that, and the check
constraint's retired-kind vocabulary is exactly the kind of thing a restatement
loses.

**`DROP SCHEMA ... CASCADE` is safe here only because nothing points into it.**
NEU-1046 repointed all five `app` and `import_ne` foreign keys onto `catalog`, so
the cascade has no inbound constraint to take with it. That was verified before
this ran, not assumed — see `tvbf-backend/docs/migration/README.md`. The dump is
the recovery path, and there is no other: this downgrade cannot restore data.
"""

from alembic import op

revision = "a7e3c8d15f42"
down_revision = "c9a3f1e60b47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tvmaze.ingest_run SET SCHEMA catalog")
    op.execute("DROP SCHEMA IF EXISTS tvmaze CASCADE")


def downgrade() -> None:
    """Puts the table back, and nothing else.

    Deliberately not a restore. The catalog data, credits and rate-budget row
    that lived in `tvmaze` are gone once `upgrade` runs, and no downgrade can
    invent them — the recovery path is the `pg_dump` taken before the drop
    (`scripts/dump_tvmaze.sh`). What this does buy is a schema that a re-run of
    `upgrade` can find, so a botched deploy is re-runnable rather than wedged.
    """
    op.execute("CREATE SCHEMA IF NOT EXISTS tvmaze")
    op.execute("ALTER TABLE catalog.ingest_run SET SCHEMA tvmaze")
