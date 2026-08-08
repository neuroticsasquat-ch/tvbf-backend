"""'add episode credit tables and season credits watermark'

Revision ID: 56f1d9ecb148
Revises: b3f7c1d9a204
Create Date: 2026-08-04 21:50:26.474591+00:00

"""
import sqlalchemy as sa
from alembic import op

revision = '56f1d9ecb148'
down_revision = 'b3f7c1d9a204'
branch_labels = None
depends_on = None

# Autogenerate also proposed dropping ix_episode_show_id_season, ix_season_show_id,
# ix_show_aka_show_id and the three trgm indexes. Those are created by raw SQL in
# earlier migrations and carry no model declaration, so autogenerate cannot see
# them; the drops are spurious and are omitted here.


def upgrade() -> None:
    op.create_table(
        'episode_crew_role',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_episode_crew_role_name'),
        schema='tvmaze',
    )
    op.create_table(
        'episode_crew',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('episode_id', sa.Integer(), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=False),
        sa.Column('role_id', sa.Integer(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['episode_id'], ['tvmaze.episode.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['person_id'], ['tvmaze.person.id']),
        sa.ForeignKeyConstraint(['role_id'], ['tvmaze.episode_crew_role.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'episode_id', 'person_id', 'role_id', name='uq_episode_crew_episode_person_role'
        ),
        schema='tvmaze',
    )
    op.create_index(
        'ix_episode_crew_episode_id_sort',
        'episode_crew',
        ['episode_id', 'sort_order'],
        schema='tvmaze',
    )
    op.create_index('ix_episode_crew_person_id', 'episode_crew', ['person_id'], schema='tvmaze')

    # Verified against prod: zero violations across the ~1.5M existing rows, so
    # this needs no cleanup step. Build the index CONCURRENTLY rather than
    # letting ADD CONSTRAINT build it — that would hold ACCESS EXCLUSIVE on the
    # table for the whole build and block ingest writes. Same reasoning as
    # c1a2f3d4e5b6. Attaching the finished index to a constraint is O(1).
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_egc_episode_person_character "
            "ON tvmaze.episode_guest_cast (episode_id, person_id, character_id)"
        )
    op.execute(
        "ALTER TABLE tvmaze.episode_guest_cast "
        "ADD CONSTRAINT uq_egc_episode_person_character "
        "UNIQUE USING INDEX uq_egc_episode_person_character"
    )

    op.add_column(
        'season',
        sa.Column('credits_synced_at', sa.DateTime(timezone=True), nullable=True),
        schema='tvmaze',
    )


def downgrade() -> None:
    op.drop_column('season', 'credits_synced_at', schema='tvmaze')
    op.drop_constraint(
        'uq_egc_episode_person_character', 'episode_guest_cast', schema='tvmaze', type_='unique'
    )
    op.drop_index('ix_episode_crew_person_id', table_name='episode_crew', schema='tvmaze')
    op.drop_index('ix_episode_crew_episode_id_sort', table_name='episode_crew', schema='tvmaze')
    op.drop_table('episode_crew', schema='tvmaze')
    op.drop_table('episode_crew_role', schema='tvmaze')
