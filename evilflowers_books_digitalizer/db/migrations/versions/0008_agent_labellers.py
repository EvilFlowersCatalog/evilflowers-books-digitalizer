"""labels.labeller_kind / labels.model, label_leases: agent labellers over MCP

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-17 02:00:00

Hand-written (evilflowers-books-digitalizer#16).

* ``labels.labeller_kind``: ``human`` (every existing row) or ``agent``;
* ``labels.model``: the model that answered, for agent labels;
* ``label_leases``: one agent's claim on an (item, task) until it expires, so parallel agents
  never label the same item twice.
"""

import sqlalchemy as sa
from alembic import op


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('labels', schema=None) as batch_op:
        batch_op.add_column(sa.Column('labeller_kind', sa.String(length=8), nullable=False,
                                      server_default='human'))
        batch_op.add_column(sa.Column('model', sa.String(length=128), nullable=True))
    op.create_table(
        'label_leases',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('item_pk', sa.Integer(), nullable=False),
        sa.Column('task', sa.String(length=32), nullable=False),
        sa.Column('labeller', sa.String(length=100), nullable=False),
        sa.Column('model', sa.String(length=128), nullable=True),
        sa.Column('leased_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['item_pk'], ['label_items.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('item_pk', 'task', name='uq_label_leases_item_task'),
    )
    with op.batch_alter_table('label_leases', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_label_leases_task'), ['task'], unique=False)
        batch_op.create_index(batch_op.f('ix_label_leases_expires_at'), ['expires_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('label_leases', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_label_leases_expires_at'))
        batch_op.drop_index(batch_op.f('ix_label_leases_task'))
    op.drop_table('label_leases')
    with op.batch_alter_table('labels', schema=None) as batch_op:
        batch_op.drop_column('model')
        batch_op.drop_column('labeller_kind')
