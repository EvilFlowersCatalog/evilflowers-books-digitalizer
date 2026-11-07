"""initial schema: books + pages

Revision ID: 0001
Revises:
Create Date: 2026-11-07
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "books",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("book_id", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(300), nullable=False, unique=True),
        sa.Column("title", sa.String(500)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("step", sa.String(64)),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("n_steps", sa.Integer(), nullable=False),
        sa.Column("n_frames", sa.Integer()),
        sa.Column("n_pages", sa.Integer()),
        sa.Column("error", sa.Text()),
        sa.Column("source_dir", sa.String(1024)),
        sa.Column("work_dir", sa.String(1024)),
        sa.Column("output_dir", sa.String(1024)),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=False),
        sa.Column("queued_at", sa.DateTime()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("source", "book_id", name="uq_books_source_book"),
    )
    op.create_index("ix_books_source", "books", ["source"])
    op.create_index("ix_books_status", "books", ["status"])
    op.create_table(
        "pages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_pk", sa.Integer(), sa.ForeignKey("books.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("source_path", sa.String(1024)),
        sa.Column("side", sa.Integer()),
        sa.Column("n_sides", sa.Integer()),
        sa.Column("x0", sa.Integer()),
        sa.Column("x1", sa.Integer()),
        sa.Column("preview_path", sa.String(1024)),
        sa.UniqueConstraint("book_pk", "index", name="uq_pages_book_index"),
    )
    op.create_index("ix_pages_book_pk", "pages", ["book_pk"])


def downgrade() -> None:
    op.drop_table("pages")
    op.drop_table("books")
