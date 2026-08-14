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

**The drop refuses unless a dump is acknowledged.** `upgrade` raises
`DumpNotVerified` when `tvmaze.show` holds real rows and `TVBF_TVMAZE_DUMP_VERIFIED`
is not set — because merging this PR *is* the drop (Coolify applies migrations on
deploy), so a runbook sentence is the only other thing standing between a merge
and an unrecoverable delete. The guard stands down on an empty schema, so a fresh
`db:init && migrate`, CI and the test suite never see it.

**`DROP SCHEMA ... CASCADE` is safe here only because nothing points into it.**
NEU-1046 repointed all five `app` and `import_ne` foreign keys onto `catalog`, so
the cascade has no inbound constraint to take with it. That was verified before
this ran, not assumed — see `tvbf-backend/docs/migration/README.md`. The dump is
the recovery path, and there is no other: this downgrade cannot restore data.
"""

import os

from alembic import op
from sqlalchemy import text

revision = "a7e3c8d15f42"
down_revision = "c9a3f1e60b47"
branch_labels = None
depends_on = None


# The acknowledgement that the pre-drop dump exists and restored. Set it in
# Coolify to the value below and deploy; the drop refuses without it.
_DUMP_ACK_ENV = "TVBF_TVMAZE_DUMP_VERIFIED"
_DUMP_ACK_VALUE = "yes"

# Below this, `tvmaze` holds nothing worth a dump and the guard stands down. A
# fresh `db:init && migrate` creates the schema empty in `8927c889e469` and a
# CI or test database never fills it, so neither is asked for a ceremony that
# would protect nothing. Production carries ~89,000 shows.
_TRIVIAL_ROWS = 1_000


class DumpNotVerified(RuntimeError):
    """The `tvmaze` schema holds real data and nothing says a dump survives it.

    Raised rather than warned, for the reason every other one-shot pass in this
    migration raises (`show_prune`'s `IngestNotRun`, `episode_repoint`'s after
    it): a pass that quietly did nothing and a pass that quietly deleted 3.5M
    episodes are the two failures a guard sits between, and only one of them is
    loud on its own. Prose in a runbook is not a guard — merging this PR *is*
    the drop, because Coolify applies migrations on deploy, so this is the last
    point at which anything can refuse.
    """


def _rows_at_risk(conn) -> int:
    present = conn.execute(
        text("SELECT to_regclass('tvmaze.show') IS NOT NULL")
    ).scalar_one()
    if not present:
        return 0
    return conn.execute(text("SELECT count(*) FROM tvmaze.show")).scalar_one()


def upgrade() -> None:
    conn = op.get_bind()
    rows = _rows_at_risk(conn)
    if rows >= _TRIVIAL_ROWS and os.environ.get(_DUMP_ACK_ENV) != _DUMP_ACK_VALUE:
        raise DumpNotVerified(
            f"tvmaze.show holds {rows:,} rows and {_DUMP_ACK_ENV} is not set to "
            f"{_DUMP_ACK_VALUE!r}. Run scripts/dump_tvmaze.sh, copy the dump and its "
            "counts file off the host, then set that variable and redeploy. "
            "See tvbf-backend/docs/migration/README.md."
        )

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
