"""pages.marks: scan artefact candidates from evilflowers-scan-service

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-16 20:00:00

Hand-written (evilflowers-books-digitalizer#4). The clean outcome of scan-service carries,
per leaf, stamp / sticker candidates (``leaves[].marks``). They are kept on the page until
the analyze stage fuses them as proposals; NULL means the page was cleaned in-process.
"""

import sqlalchemy as sa
from alembic import op


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('pages', sa.Column('marks', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('pages', 'marks')
