"""runs: liveness columns for the stale-run sweeper (heartbeat_at, dispatched_at)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07 18:00:00

Hand-written. Both columns are nullable timestamps without a server default:
an existing row reads NULL, which the sweeper treats as "use created_at /
started_at" (never as "fresh").
"""

import sqlalchemy as sa
from alembic import op


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('heartbeat_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('dispatched_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_column('dispatched_at')
        batch_op.drop_column('heartbeat_at')
