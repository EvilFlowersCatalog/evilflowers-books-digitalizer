"""Unit of work: one SQLAlchemy transaction per use case, events published after commit.

This is the application layer's only door to persistence (dependency rule in
docs/design/studio-spec.md): use cases import the session and the ORM models
from here, never from ``db`` directly, so swapping the persistence adapter
touches one module.

Events are *notifications* of committed state. A client that reacts to
``book.queued`` re-reads over REST, so publishing before the commit would race
it; :meth:`UnitOfWork.emit` therefore buffers until the transaction succeeds.
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import UnaryExpression

from evilflowers_books_digitalizer.db.models import (
    Book,
    Note,
    Page,
    Publication,
    Region,
    Review,
    Run,
    utcnow,
)
from evilflowers_books_digitalizer.studio.events import Event
from evilflowers_books_digitalizer.studio.ports import EventBus

#: the query vocabulary use cases may use (re-exported so SQLAlchemy enters only here)
__all__ = ["Book", "ColumnElement", "Note", "Page", "Publication", "Region", "Review", "Run",
           "Session", "SessionFactory", "UnaryExpression", "UnitOfWork", "func", "or_", "select",
           "utcnow"]

type SessionFactory = Callable[[], Session]


class UnitOfWork:
    """``with UnitOfWork(factory, bus) as uow: uow.session…`` — commit on success, else roll back."""

    def __init__(self, session_factory: SessionFactory, bus: EventBus | None = None) -> None:
        self._factory = session_factory
        self._bus = bus
        self._pending: list[Event] = []
        self.session: Session

    def __enter__(self) -> UnitOfWork:
        self.session = self._factory()
        # use cases build DTOs from rows after the commit; expiring them would force a reload
        self.session.expire_on_commit = False
        self._pending = []
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()
        if exc_type is None:
            self._flush_events()

    def emit(self, event: Event) -> None:
        """Queue an event; it is published only if the transaction commits."""
        self._pending.append(event)

    def _flush_events(self) -> None:
        if self._bus is None:
            return
        for event in self._pending:
            self._bus.publish(event)
        self._pending = []
