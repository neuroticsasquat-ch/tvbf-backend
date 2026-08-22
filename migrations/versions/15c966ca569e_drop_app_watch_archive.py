"""drop app.watch_archive

Revision ID: 15c966ca569e
Revises: d5a91c4e2f68
Create Date: 2026-08-21 00:00:00.000000+00:00

"""

import os

from alembic import op
from sqlalchemy import text

revision = "15c966ca569e"
down_revision = "d5a91c4e2f68"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    row_count = conn.execute(text("SELECT count(*) FROM app.watch_archive")).scalar()

    if row_count > 0 and os.environ.get("TVBF_WATCH_ARCHIVE_DUMP_VERIFIED") != "yes":
        raise RuntimeError(
            f"watch_archive has {row_count} row(s) but TVBF_WATCH_ARCHIVE_DUMP_VERIFIED "
            "is not set. Run scripts/dump_watch_archive.sh against production first, "
            "store the dump off the VM, then set TVBF_WATCH_ARCHIVE_DUMP_VERIFIED=yes "
            "in Coolify and re-deploy."
        )

    op.execute("DROP TABLE IF EXISTS app.watch_archive CASCADE")
    op.execute("DROP FUNCTION IF EXISTS app.watch_archive_no_mutation")
    op.execute("DROP TYPE IF EXISTS app.watch_archive_record_type")


def downgrade() -> None:
    # The table and trigger were created by c9f2b7a41d38. Reversing the drop
    # requires restoring from the pre-drop dump rather than rebuilding the schema
    # here, because the migration that created the table is still in the chain
    # and a fresh database already runs through it. Leaving downgrade as a no-op
    # is the deliberate shape: Alembic still accepts this as a valid migration,
    # and the pre-drop dump is the recovery path.
    pass
