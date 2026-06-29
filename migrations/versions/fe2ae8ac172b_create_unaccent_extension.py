"""'create unaccent extension'

Revision ID: fe2ae8ac172b
Revises: c71bc6a53424
Create Date: 2026-06-29 16:50:01.419343+00:00

"""
from alembic import op


revision = 'fe2ae8ac172b'
down_revision = 'c71bc6a53424'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")


def downgrade() -> None:
    # Leave the extension installed — harmless to keep and other objects may
    # depend on it. No-op downgrade.
    pass
