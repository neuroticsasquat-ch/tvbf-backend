"""'retire person_initial: drop person.credits_synced_at and the run kind'

Revision ID: a7c0d21e5f38
Revises: 56f1d9ecb148
Create Date: 2026-08-04 22:45:00.000000+00:00

NEU-962. Two deliberate losses, recorded here so neither is discovered later:

1. `tvmaze.person.credits_synced_at` was the retired pass C's resumability
   watermark and the only per-row record of which people that pass reached
   (126,706 of 486,790 before it was cancelled on 2026-08-04). Nothing else ever
   read it. The count survives on the cancelled `ingest_run` row; the per-person
   detail does not, and is not worth a vestigial column.

2. `ck_ingest_run_kind` no longer admits `person_initial`. The constraint is
   re-added NOT VALID on purpose: prod holds that cancelled run row, and a
   validated constraint would refuse to attach over it. NOT VALID leaves
   historical rows readable while rejecting any new `person_initial` write,
   which is the whole point. On a fresh database there are no such rows and the
   distinction is invisible.

"""
from alembic import op
import sqlalchemy as sa

revision = 'a7c0d21e5f38'
down_revision = '56f1d9ecb148'
branch_labels = None
depends_on = None

_KINDS_WITHOUT_PERSON_INITIAL = (
    "'initial', 'update', 'akas_backfill', 'ratings_backfill', "
    "'show_refresh', 'person_update'"
)
_KINDS_WITH_PERSON_INITIAL = (
    "'initial', 'update', 'akas_backfill', 'ratings_backfill', "
    "'show_refresh', 'person_initial', 'person_update'"
)


def upgrade() -> None:
    op.drop_column('person', 'credits_synced_at', schema='tvmaze')
    op.execute('ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind')
    op.execute(
        'ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind '
        f'CHECK (kind IN ({_KINDS_WITHOUT_PERSON_INITIAL})) NOT VALID'
    )


def downgrade() -> None:
    op.execute('ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind')
    op.execute(
        'ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind '
        f'CHECK (kind IN ({_KINDS_WITH_PERSON_INITIAL}))'
    )
    op.add_column(
        'person',
        sa.Column('credits_synced_at', sa.DateTime(timezone=True), nullable=True),
        schema='tvmaze',
    )
