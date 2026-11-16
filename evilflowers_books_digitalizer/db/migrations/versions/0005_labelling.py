"""label_items, labels: hand labels for decision-model calibration

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16 18:00:00

Hand-written (evilflowers-books-digitalizer#7). Catalog items sampled for labelling
(figures, tables, section pairs, entries) and one answer per (item, task, labeller), with
the interface it came through (``via``: spa, cli, mcp). Nothing references the studio's
own books.
"""

import sqlalchemy as sa
from alembic import op


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'label_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('key', sa.String(length=255), nullable=False),
        sa.Column('ref', sa.JSON(), nullable=False),
        sa.Column('language', sa.String(length=8), nullable=True),
        sa.Column('stratum', sa.String(length=64), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('hidden', sa.JSON(), nullable=False),
        sa.Column('sampled_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key'),
    )
    with op.batch_alter_table('label_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_label_items_kind'), ['kind'], unique=False)
    op.create_table(
        'labels',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('item_pk', sa.Integer(), nullable=False),
        sa.Column('task', sa.String(length=32), nullable=False),
        sa.Column('value', sa.String(length=64), nullable=False),
        sa.Column('labeller', sa.String(length=100), nullable=False),
        sa.Column('via', sa.String(length=8), nullable=False),
        sa.Column('labelled_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['item_pk'], ['label_items.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('item_pk', 'task', 'labeller', name='uq_labels_item_task_labeller'),
    )
    with op.batch_alter_table('labels', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_labels_item_pk'), ['item_pk'], unique=False)
        batch_op.create_index(batch_op.f('ix_labels_task'), ['task'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('labels', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_labels_task'))
        batch_op.drop_index(batch_op.f('ix_labels_item_pk'))
    op.drop_table('labels')
    with op.batch_alter_table('label_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_label_items_kind'))
    op.drop_table('label_items')
