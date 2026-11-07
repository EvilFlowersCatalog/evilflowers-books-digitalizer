"""``studio.discover``: the scan tree -> ``books`` rows (status ``discovered``).

Books are keyed by their permanent IA identifier. A row the classic batch runner
wrote for the same directory (same ``source`` + ``book_id``, no project) is
adopted instead of duplicated, so its history stays attached.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from evilflowers_books_digitalizer.db.models import Book
from evilflowers_books_digitalizer.studio.domain import BookStatus
from evilflowers_books_digitalizer.studio.events import EventType
from evilflowers_books_digitalizer.studio.ports import ScannedBook
from evilflowers_books_digitalizer.studio.workers.context import Uow, WorkerContext


def _find(uow: Uow, item: str, sb: ScannedBook) -> Book | None:
    s = uow.session
    return (s.scalar(select(Book).where(Book.item_id == item))
            or s.scalar(select(Book).where(Book.source == sb.collection,
                                           Book.book_id == sb.external_id)))


def upsert(uow: Uow, project: str, item: str, sb: ScannedBook) -> tuple[Book, bool]:
    """Insert or refresh one book; returns ``(book, created)``."""
    book = _find(uow, item, sb)
    created = book is None
    if book is None:
        book = Book(source=sb.collection, book_id=sb.external_id, slug=item,
                    status=BookStatus.DISCOVERED, artifacts={}, stats={},
                    item_metadata={"identifier": item, "collection": [sb.collection]})
        uow.session.add(book)
    book.project = project
    book.item_id = book.item_id or item
    book.source_dir = str(sb.path)
    book.n_frames = len(sb.frames)
    uow.session.flush()
    return book, created


def discover(ctx: WorkerContext, project: str, collection: str | None = None) -> dict[str, Any]:
    cfg = ctx.project(project)
    store = ctx.adapters(project).scans
    collections = [collection] if collection else store.collections()
    summary: dict[str, Any] = {}
    for coll in collections:
        with ctx.uow() as uow:
            uow.emit(EventType.IMPORT_STARTED, project, collection=coll)
            seen = new = 0
            for sb in store.books(coll):
                book, created = upsert(uow, project, cfg.item_identifier(coll, sb.external_id), sb)
                seen += 1
                if created:
                    new += 1
                    uow.emit(EventType.BOOK_DISCOVERED, project, book.id, slug=book.slug)
            uow.emit(EventType.IMPORT_FINISHED, project, collection=coll, books=seen, new=new)
        summary[coll] = {"books": seen, "new": new}
    return summary
