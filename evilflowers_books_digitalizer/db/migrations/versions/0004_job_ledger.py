"""jobs: the orchestrator's job ledger; runs lose the canvas-era liveness columns

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16 14:00:00

Hand-written. A run no longer sends a Celery canvas: each stage is planned as
``jobs`` rows, dispatched by the scheduler's pump with ``link``/``link_error``
callbacks, and settled from those callbacks (or by ``digitalizer.reconcile``).
Liveness moves from the run to the job, so ``runs.heartbeat_at`` and
``runs.dispatched_at`` (0003) go. ``runs.scheduled_at`` is new: when the pump last
sent ``digitalizer.run`` for a queued run (NULL while admission holds it, e.g. a
publish that waits for its render). Runs that were mid-flight under the old canvas
cannot be resumed by the ledger: drain the queues before upgrading.
"""

import sqlalchemy as sa
from alembic import op


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_pk', sa.Integer(), nullable=False),
        sa.Column('stage', sa.String(length=32), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('lane', sa.String(length=16), nullable=False),
        sa.Column('backend', sa.String(length=16), nullable=False),
        sa.Column('task', sa.String(length=128), nullable=False),
        sa.Column('queue', sa.String(length=128), nullable=False),
        sa.Column('params', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('token', sa.String(length=64), nullable=True),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('budget', sa.Integer(), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('dispatched_at', sa.DateTime(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
        sa.Column('deadline_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('summary', sa.JSON(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['run_pk'], ['runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_jobs_run_pk'), ['run_pk'], unique=False)
        batch_op.create_index(batch_op.f('ix_jobs_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_jobs_task_id'), ['task_id'], unique=False)
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_column('dispatched_at')
        batch_op.drop_column('heartbeat_at')
        batch_op.add_column(sa.Column('scheduled_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_column('scheduled_at')
        batch_op.add_column(sa.Column('heartbeat_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('dispatched_at', sa.DateTime(), nullable=True))
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_jobs_task_id'))
        batch_op.drop_index(batch_op.f('ix_jobs_status'))
        batch_op.drop_index(batch_op.f('ix_jobs_run_pk'))
    op.drop_table('jobs')
