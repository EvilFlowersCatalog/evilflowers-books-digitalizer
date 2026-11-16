"""mark_groups (scan marks across a book) and books.metadata_suggestions / metadata_confirmed

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-16 23:00:00

Hand-written (evilflowers-books-digitalizer#14, #15).

* ``mark_groups``: one distinct scan mark of a book (dHash + aspect), its verdict and who gave
  it; members are the ``pages.marks`` rows carrying its id as ``group``;
* ``books.metadata_suggestions``: title, authors, publisher, year the decider picked from the
  title page's OCR lines, never imported until the operator confirms them;
* ``books.metadata_confirmed``: the operator's confirmation, a labelled case for #7.
"""

import sqlalchemy as sa
from alembic import op


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'mark_groups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('book_pk', sa.Integer(), nullable=False),
        sa.Column('category', sa.String(length=16), nullable=False),
        sa.Column('dhash', sa.String(length=16), nullable=False),
        sa.Column('aspect', sa.Float(), nullable=False),
        sa.Column('leaves', sa.Integer(), nullable=False),
        sa.Column('members', sa.Integer(), nullable=False),
        sa.Column('band', sa.String(length=8), nullable=False),
        sa.Column('decision', sa.String(length=32), nullable=True),
        sa.Column('p', sa.Float(), nullable=True),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('decider_model', sa.String(length=128), nullable=True),
        sa.Column('crop_leaf', sa.Integer(), nullable=True),
        sa.Column('crop_bbox', sa.JSON(), nullable=True),
        sa.Column('decider_error', sa.String(length=500), nullable=True),
        sa.Column('split_from', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['book_pk'], ['books.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_mark_groups_book_pk'), 'mark_groups', ['book_pk'], unique=False)
    op.add_column('books', sa.Column('metadata_suggestions', sa.JSON(), nullable=True))
    op.add_column('books', sa.Column('metadata_confirmed', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('books', 'metadata_confirmed')
    op.drop_column('books', 'metadata_suggestions')
    op.drop_index(op.f('ix_mark_groups_book_pk'), table_name='mark_groups')
    op.drop_table('mark_groups')
