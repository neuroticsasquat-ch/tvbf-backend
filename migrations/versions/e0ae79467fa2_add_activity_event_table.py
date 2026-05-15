"""add activity_event table

Revision ID: e0ae79467fa2
Revises: 68afd4fae961
Create Date: 2026-05-15 21:30:46.427805+00:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'e0ae79467fa2'
down_revision = '68afd4fae961'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'activity_event',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('actor_id', sa.UUID(), nullable=False),
        sa.Column('verb', sa.Text(), nullable=False),
        sa.Column('target_type', sa.Text(), nullable=False),
        sa.Column('target_id', sa.BigInteger(), nullable=False),
        sa.Column('season_number', sa.Integer(), nullable=True),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['actor_id'], ['app.user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'actor_id',
            'verb',
            'target_type',
            'target_id',
            'season_number',
            name='uq_activity_event',
            postgresql_nulls_not_distinct=True,
        ),
        schema='app',
    )
    op.create_index(
        'ix_activity_event_actor_created',
        'activity_event',
        ['actor_id', 'created_at'],
        unique=False,
        schema='app',
    )
    op.create_index(
        'ix_activity_event_target',
        'activity_event',
        ['target_type', 'target_id'],
        unique=False,
        schema='app',
    )


def downgrade() -> None:
    op.drop_index('ix_activity_event_target', table_name='activity_event', schema='app')
    op.drop_index('ix_activity_event_actor_created', table_name='activity_event', schema='app')
    op.drop_table('activity_event', schema='app')
