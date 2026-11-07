"""Exports: an Internet Archive item zip for one book, the training set for a project.

The ``ports.IaExporter`` takes plain data, so the IA adapter needs no database
access: this use case reads the rows and hands over metadata, leaves and files.
"""

from __future__ import annotations

from pathlib import Path

from evilflowers_books_digitalizer.studio.application import dto
from evilflowers_books_digitalizer.studio.application.books import artifact_path, item_metadata_of
from evilflowers_books_digitalizer.studio.application.errors import Conflict, NotFound
from evilflowers_books_digitalizer.studio.application.lookup import get_book
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.metadata import leaf_from_row
from evilflowers_books_digitalizer.studio.ports import IA_KINDS, IaExporter

__all__ = ["export_ia", "export_training"]


def export_ia(svc: Services, exporter: IaExporter, book_id: int, out_dir: Path) -> Path:
    """Write ``<out_dir>/<item_id>_ia.zip``; the book must have an identifier and leaves."""
    with svc.uow() as uow:
        book = get_book(uow.session, book_id)
        if not book.item_id:
            raise Conflict(f"book {book_id} has no IA identifier (not a studio book)")
        if not book.pages:
            raise Conflict(f"book {book_id} has no pages yet")
        store = svc.artifacts_for(book.project) if book.project else None
        files = {kind: Path(p) for kind in IA_KINDS
                 if (p := artifact_path(book, kind, store)) and Path(p).exists()}
        leaves = [leaf_from_row(p) for p in book.pages]
        images = {p.index: Path(p.clean_path) for p in book.pages
                  if p.clean_path and Path(p.clean_path).exists()}
        item_id, metadata = book.item_id, item_metadata_of(book)
    out_dir.mkdir(parents=True, exist_ok=True)
    return exporter.export_item(item_id, metadata, leaves, files, images,
                                out_dir / f"{item_id}_ia.zip")


def export_training(svc: Services, project: str) -> dto.TaskOut:
    """Enqueue ``studio.export_training`` (confirmed regions -> COCO / DocLayNet)."""
    if svc.project(project) is None:
        raise NotFound(f"unknown project {project!r}")
    return dto.TaskOut(task_id=svc.queue.enqueue_export(project))
