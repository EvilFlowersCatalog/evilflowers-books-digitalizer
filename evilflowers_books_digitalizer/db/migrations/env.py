"""Alembic environment — configured programmatically by ``db.store`` (no alembic.ini)."""

from alembic import context

from evilflowers_books_digitalizer.db.models import Base
from evilflowers_books_digitalizer.db.store import get_engine

config = context.config
target_metadata = Base.metadata
URL = config.get_main_option("sqlalchemy.url")


def run_offline() -> None:
    context.configure(url=URL, target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    with get_engine(URL).connect() as connection:
        # render_as_batch: SQLite can't ALTER most things; batch mode rebuilds tables
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
