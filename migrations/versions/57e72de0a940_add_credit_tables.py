"""'add credit tables'

Revision ID: 57e72de0a940
Revises: c2e451aa1ec6
Create Date: 2026-08-02 03:03:59.977804+00:00

"""
import sqlalchemy as sa
from alembic import op

revision = '57e72de0a940'
down_revision = 'c2e451aa1ec6'
branch_labels = None
depends_on = None

_OLD_KINDS = "'initial', 'update', 'akas_backfill', 'ratings_backfill'"
_NEW_KINDS = (
    "'initial', 'update', 'akas_backfill', 'ratings_backfill', "
    "'show_refresh', 'person_initial', 'person_update'"
)


def upgrade() -> None:
    op.create_table(
        'person',
        sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('country_code', sa.Text(), nullable=True),
        sa.Column('country_name', sa.Text(), nullable=True),
        sa.Column('timezone', sa.Text(), nullable=True),
        sa.Column('birthday', sa.Date(), nullable=True),
        sa.Column('deathday', sa.Date(), nullable=True),
        sa.Column('gender', sa.Text(), nullable=True),
        sa.Column('image_medium', sa.Text(), nullable=True),
        sa.Column('image_original', sa.Text(), nullable=True),
        sa.Column('tvmaze_updated', sa.BigInteger(), nullable=False),
        sa.Column(
            'ingested_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('credits_synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        schema='tvmaze',
    )
    op.create_table(
        'character',
        sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('image_medium', sa.Text(), nullable=True),
        sa.Column('image_original', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        schema='tvmaze',
    )
    op.create_table(
        'crew_role',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_crew_role_name'),
        schema='tvmaze',
    )

    # Credit tables. No unique constraints — refresh is delete-then-insert,
    # so there is nothing to conflict on (see tvmaze.season for the lesson).
    op.create_table(
        'show_cast',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('show_id', sa.Integer(), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=False),
        sa.Column('character_id', sa.Integer(), nullable=False),
        sa.Column('is_self', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('is_voice', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['character_id'], ['tvmaze.character.id']),
        sa.ForeignKeyConstraint(['person_id'], ['tvmaze.person.id']),
        sa.ForeignKeyConstraint(['show_id'], ['tvmaze.show.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        schema='tvmaze',
    )
    op.create_index(
        'ix_show_cast_show_id_sort', 'show_cast', ['show_id', 'sort_order'], schema='tvmaze'
    )
    op.create_index('ix_show_cast_person_id', 'show_cast', ['person_id'], schema='tvmaze')

    op.create_table(
        'show_crew',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('show_id', sa.Integer(), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=False),
        sa.Column('role_id', sa.Integer(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['person_id'], ['tvmaze.person.id']),
        sa.ForeignKeyConstraint(['role_id'], ['tvmaze.crew_role.id']),
        sa.ForeignKeyConstraint(['show_id'], ['tvmaze.show.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        schema='tvmaze',
    )
    op.create_index(
        'ix_show_crew_show_id_sort', 'show_crew', ['show_id', 'sort_order'], schema='tvmaze'
    )
    op.create_index('ix_show_crew_person_id', 'show_crew', ['person_id'], schema='tvmaze')

    op.create_table(
        'episode_guest_cast',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('episode_id', sa.Integer(), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=False),
        sa.Column('character_id', sa.Integer(), nullable=False),
        sa.Column('is_self', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('is_voice', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['character_id'], ['tvmaze.character.id']),
        sa.ForeignKeyConstraint(['episode_id'], ['tvmaze.episode.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['person_id'], ['tvmaze.person.id']),
        sa.PrimaryKeyConstraint('id'),
        schema='tvmaze',
    )
    op.create_index(
        'ix_egc_episode_id_sort',
        'episode_guest_cast',
        ['episode_id', 'sort_order'],
        schema='tvmaze',
    )
    op.create_index('ix_egc_person_id', 'episode_guest_cast', ['person_id'], schema='tvmaze')

    op.add_column(
        'show',
        sa.Column('credits_synced_at', sa.DateTime(timezone=True), nullable=True),
        schema='tvmaze',
    )

    # Widen kind and rewrite the whitelist. Drop before alter — the constraint
    # references the column being changed.
    op.execute("ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.alter_column(
        'ingest_run',
        'kind',
        existing_type=sa.VARCHAR(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
        schema='tvmaze',
    )
    op.execute(
        "ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        f"CHECK (kind IN ({_NEW_KINDS}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.alter_column(
        'ingest_run',
        'kind',
        existing_type=sa.String(length=32),
        type_=sa.VARCHAR(length=16),
        existing_nullable=False,
        schema='tvmaze',
    )
    op.execute(
        "ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        f"CHECK (kind IN ({_OLD_KINDS}))"
    )

    op.drop_column('show', 'credits_synced_at', schema='tvmaze')

    op.drop_index('ix_egc_person_id', table_name='episode_guest_cast', schema='tvmaze')
    op.drop_index('ix_egc_episode_id_sort', table_name='episode_guest_cast', schema='tvmaze')
    op.drop_table('episode_guest_cast', schema='tvmaze')

    op.drop_index('ix_show_crew_person_id', table_name='show_crew', schema='tvmaze')
    op.drop_index('ix_show_crew_show_id_sort', table_name='show_crew', schema='tvmaze')
    op.drop_table('show_crew', schema='tvmaze')

    op.drop_index('ix_show_cast_person_id', table_name='show_cast', schema='tvmaze')
    op.drop_index('ix_show_cast_show_id_sort', table_name='show_cast', schema='tvmaze')
    op.drop_table('show_cast', schema='tvmaze')

    op.drop_table('crew_role', schema='tvmaze')
    op.drop_table('character', schema='tvmaze')
    op.drop_table('person', schema='tvmaze')
