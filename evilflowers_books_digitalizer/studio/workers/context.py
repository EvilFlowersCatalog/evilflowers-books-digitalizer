"""Worker runtime: config, database sessions, event bus and per-project adapters.

One process-wide :class:`WorkerContext`, built lazily from ``studio.config`` on the
first task — never at import time, so importing the Celery app (API process,
tests) needs neither a config file nor a database. Tests install their own with
:func:`configure` (SQLite + in-memory bus + fakes).

:class:`Uow` is the unit of work every stage uses: one SQLAlchemy session plus an
event *outbox* flushed only after a successful commit, so an SSE client that
reacts to an event and re-reads over REST always sees the committed state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from evilflowers_books_digitalizer.studio.config import ProjectConfig, StudioConfig
from evilflowers_books_digitalizer.studio.events import Event, EventType
from evilflowers_books_digitalizer.studio.ports import EventBus
from evilflowers_books_digitalizer.studio.adapters.registry import Adapters, build_adapters

type AdaptersFactory = Callable[[StudioConfig, ProjectConfig], Adapters]


@dataclass
class Uow:
    session: Session
    events: list[Event] = field(default_factory=list)

    def emit(self, type_: EventType, project: str, book_id: int | None = None,
             **data: Any) -> None:
        self.events.append(Event(type_, project, book_id, data))


@dataclass
class WorkerContext:
    config: StudioConfig
    session_factory: Callable[[], Session]
    bus: EventBus
    adapters_factory: AdaptersFactory = build_adapters
    _adapters: dict[str, Adapters] = field(default_factory=dict)

    def project(self, slug: str) -> ProjectConfig:
        return self.config.project(slug)

    def adapters(self, slug: str) -> Adapters:
        """Adapters are built once per project and process (models, catalogs stay loaded)."""
        if slug not in self._adapters:
            self._adapters[slug] = self.adapters_factory(self.config, self.project(slug))
        return self._adapters[slug]

    def work_dir(self, project: str, item_id: str) -> Path:
        """Scratch for one book: cleaned TIFFs, per-leaf hOCR, VLM cache (not an artifact)."""
        return self.config.cache_dir / project / item_id

    @contextmanager
    def uow(self) -> Iterator[Uow]:
        session = self.session_factory()
        unit = Uow(session)
        try:
            yield unit
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()
        for event in unit.events:   # after commit only: events are notifications of state
            self.bus.publish(event)


_current: WorkerContext | None = None


def configure(ctx: WorkerContext | None) -> None:
    """Install (or with ``None`` drop) the process-wide context — tests and worker boot."""
    global _current
    _current = ctx


def from_config(config: StudioConfig) -> WorkerContext:
    """Production wiring: SQLAlchemy engine on ``database_url``, Redis pub/sub bus."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from evilflowers_books_digitalizer.studio.adapters.eventbus import RedisEventBus

    engine = create_engine(config.database_url, pool_pre_ping=True)
    return WorkerContext(config, sessionmaker(engine, expire_on_commit=False),
                         RedisEventBus(config.redis_url))


def current() -> WorkerContext:
    global _current
    if _current is None:
        from evilflowers_books_digitalizer.studio.config import load_config

        _current = from_config(load_config())
    return _current
