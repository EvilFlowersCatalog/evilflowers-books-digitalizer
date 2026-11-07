"""``studio.publish``: rendered files -> CatalogPublisher -> ``publications`` row.

Republishing updates the same catalog entry (its id comes from the latest
Publication of the same target) and appends a row with ``version + 1``, so the
history of what was live when stays queryable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from evilflowers_books_digitalizer.db.models import Publication
from evilflowers_books_digitalizer.studio.domain import BookStatus, check_transition
from evilflowers_books_digitalizer.studio.events import EventType
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.ports import PortError, PublishResult
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import WorkerContext
from evilflowers_books_digitalizer.studio.workers.stages import common


@dataclass(frozen=True, slots=True)
class PublishJob:
    project: str
    target: str
    item_id: str
    metadata: ItemMetadata
    files: dict[str, Path]
    entry_id: str | None
    version: int


def _job(ctx: WorkerContext, run_id: int) -> PublishJob | None:
    """What to publish; ``None`` when a previous attempt already recorded it."""
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        book = run.book
        runs.enter_stage(uow, run, "publish", 1)
        if runs.stage_done(run):
            return None             # retry after a failed finish: never publish twice
        project = ctx.project(runs.project_of(book))
        target = project.publish.target
        # fail before any network call when the book may not be published
        check_transition(book.status, BookStatus.PUBLISHED)
        latest = uow.session.scalar(
            select(Publication).where(Publication.book_pk == book.id, Publication.target == target)
            .order_by(Publication.version.desc()).limit(1))
        artifacts = book.artifacts or {}
        files = {fmt: Path(artifacts[fmt]) for fmt in project.outputs
                 if fmt in artifacts and Path(artifacts[fmt]).exists()}
        if not files:
            raise PortError(f"{book.item_id}: nothing rendered to publish")
        return PublishJob(project.slug, target, common.item_id(book), common.book_metadata(book),
                          files, latest.entry_id if latest else None,
                          (latest.version + 1) if latest else 1)


def _record(ctx: WorkerContext, run_id: int, job: PublishJob, result: PublishResult) -> None:
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        book = run.book
        pub = Publication(target=job.target, entry_id=result.entry_id, version=job.version,
                          url=result.url, author=run.created_by,
                          files={fmt: {"path": str(path), "acquisition": result.acquisitions.get(fmt)}
                                 for fmt, path in job.files.items()})
        book.publications.append(pub)
        runs.move_book(uow, book, BookStatus.PUBLISHED)
        uow.session.flush()
        uow.emit(EventType.BOOK_PUBLISHED, job.project, book.id, publication_id=pub.id,
                 entry_id=pub.entry_id, version=pub.version)
        runs.advance(uow, run)
        runs.log(uow, run, f"published {job.item_id} as {pub.entry_id} v{pub.version}"
                           f" ({'created' if result.created else 'updated'})")


def publish(ctx: WorkerContext, run_id: int) -> None:
    job = _job(ctx, run_id)
    if job is None:
        return
    publisher = ctx.adapters(job.project).publisher
    if publisher is None:
        raise PortError(f"project {job.project} has no publish target")
    result = publisher.publish(job.item_id, job.metadata, job.files, entry_id=job.entry_id)
    _record(ctx, run_id, job, result)
