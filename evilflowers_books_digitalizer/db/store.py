"""Engine/session handling and migrations.

The URL is the studio's ``database_url`` (``[studio]`` or ``$STUDIO_DATABASE_URL``); Postgres
in every deployment, SQLite for a laptop. SQLite runs in WAL mode with a busy timeout.

Schema changes go through Alembic (``evilflowers_books_digitalizer/db/migrations``), driven
programmatically: ``evilflowers-digitalizer studio db-upgrade`` (or ``db upgrade``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

import sqlalchemy
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

MIGRATIONS = Path(__file__).with_name("migrations")


@lru_cache(maxsize=8)
def get_engine(url: str) -> Engine:
    # ``sqlalchemy.create_engine`` looked up at call time, not imported by name: the
    # OpenTelemetry instrumentation wraps that attribute (studio.telemetry), and a name
    # bound at import would bypass it and leave every query untraced (#13)
    create_engine = sqlalchemy.create_engine
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"timeout": 30})

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


@contextmanager
def session_scope(url: str) -> Iterator[Session]:
    session = sessionmaker(get_engine(url), expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _alembic_config(url: str) -> Any:
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    """Apply migrations up to ``revision`` (idempotent)."""
    logging.getLogger("alembic").setLevel(logging.WARNING)   # before import: plugins log on load
    from alembic import command

    command.upgrade(_alembic_config(url), revision)


def revision(url: str, message: str) -> None:
    """Autogenerate a new migration from model changes (developer command)."""
    from alembic import command

    command.revision(_alembic_config(url), message=message, autogenerate=True)


def current_revision(url: str) -> str | None:
    from alembic.runtime.migration import MigrationContext

    with get_engine(url).connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()
