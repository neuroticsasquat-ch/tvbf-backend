"""'admit episode_credits_backfill as an ingest_run kind'

Revision ID: d41c8a6b7e92
Revises: a7c0d21e5f38
Create Date: 2026-08-04 23:30:00.000000+00:00

NEU-961. Written against NEU-962's kind list, not today's — two tickets edit
this constraint in the same release, and this one is authored second, so the
list below already omits `person_initial`.

Re-added NOT VALID for the same reason NEU-962 did: prod holds the cancelled
pass-C run row, and a validated constraint would refuse to attach over it.
NOT VALID skips only the scan of existing rows; writes are enforced either way.

"""
from alembic import op

revision = 'd41c8a6b7e92'
down_revision = 'a7c0d21e5f38'
branch_labels = None
depends_on = None

_KINDS_WITH_EPISODE_CREDITS = (
    "'initial', 'update', 'akas_backfill', 'ratings_backfill', "
    "'show_refresh', 'person_update', 'episode_credits_backfill'"
)
_KINDS_WITHOUT_EPISODE_CREDITS = (
    "'initial', 'update', 'akas_backfill', 'ratings_backfill', "
    "'show_refresh', 'person_update'"
)


def upgrade() -> None:
    op.execute('ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind')
    op.execute(
        'ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind '
        f'CHECK (kind IN ({_KINDS_WITH_EPISODE_CREDITS})) NOT VALID'
    )


def downgrade() -> None:
    op.execute('ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind')
    op.execute(
        'ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind '
        f'CHECK (kind IN ({_KINDS_WITHOUT_EPISODE_CREDITS})) NOT VALID'
    )
