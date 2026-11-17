"""The studio database: migrations match the models."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from evilflowers_books_digitalizer.db import upgrade
from evilflowers_books_digitalizer.db.models import Base
from evilflowers_books_digitalizer.db.store import current_revision, get_engine


def _url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


def test_migrations_create_exactly_the_model_schema(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    url = _url(tmp_path)
    upgrade(url)
    upgrade(url)  # idempotent
    assert current_revision(url) == "0008"
    with get_engine(url).connect() as conn:
        assert set(inspect(conn).get_table_names()) >= {
            "books", "pages", "alembic_version", "regions", "runs", "jobs", "notes", "reviews",
            "publications", "label_items", "labels", "mark_groups", "label_leases"}
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_downgrade_to_0006_and_back(tmp_path):
    from alembic import command

    from evilflowers_books_digitalizer.db.store import _alembic_config

    url = _url(tmp_path)
    upgrade(url)
    command.downgrade(_alembic_config(url), "0006")
    with get_engine(url).connect() as conn:
        assert "mark_groups" not in inspect(conn).get_table_names()
    upgrade(url)
    assert current_revision(url) == "0008"


def test_0008_keeps_existing_labels_human(tmp_path):
    """Labels written before #16 become human labels; 0008 downgrades cleanly."""
    from alembic import command
    from sqlalchemy import text

    from evilflowers_books_digitalizer.db.store import _alembic_config

    url = _url(tmp_path)
    command.upgrade(_alembic_config(url), "0007")
    with get_engine(url).begin() as conn:
        conn.execute(text("INSERT INTO label_items (id, kind, key, ref, payload, hidden, sampled_at) "
                          "VALUES (1, 'figure', 'figure:e:r', '{}', '{}', '{}', '2026-11-16')"))
        conn.execute(text("INSERT INTO labels (item_pk, task, value, labeller, via, labelled_at) "
                          "VALUES (1, 'figure_kind', 'chart', 'jana', 'spa', '2026-11-16')"))
    upgrade(url)
    with get_engine(url).connect() as conn:
        assert conn.execute(text("SELECT labeller_kind, model FROM labels")).one() == ("human", None)
    command.downgrade(_alembic_config(url), "0007")
    with get_engine(url).connect() as conn:
        assert "label_leases" not in inspect(conn).get_table_names()
