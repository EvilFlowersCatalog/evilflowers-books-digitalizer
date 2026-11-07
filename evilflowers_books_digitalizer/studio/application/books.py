"""Use cases around books: projects overview, import, listing, detail, metadata edits.

Conventions assumed from discovery (``studio.discover``, batch side): a studio
book row has ``project`` = project slug, ``source`` = collection key and
``book_id`` = the book directory name (``ScannedBook.external_id``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.studio.application import dto
from evilflowers_books_digitalizer.studio.application.errors import Invalid, NotFound
from evilflowers_books_digitalizer.studio.application.item_metadata import apply_patch
from evilflowers_books_digitalizer.studio.application.lookup import get_book as load_book
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.application.uow import (
    Book,
    ColumnElement,
    Page,
    Region,
    Session,
    UnaryExpression,
    func,
    or_,
    select,
)
from evilflowers_books_digitalizer.studio.domain import RegionStatus
from evilflowers_books_digitalizer.studio.events import Event, EventType
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.ports import ARTIFACT_KINDS, IA_KINDS, ArtifactStore

_SORTS = {"id": Book.id, "title": Book.title, "status": Book.status,
          "quality_score": Book.quality_score, "updated_at": Book.updated_at,
          "item_id": Book.item_id}


# ---------------------------------------------------------------- projects
def list_projects(svc: Services) -> list[dto.ProjectOut]:
    with svc.uow() as uow:
        rows = uow.session.execute(
            select(Book.project, Book.source, Book.status, func.count())
            .where(Book.project.in_(list(svc.config.projects)))
            .group_by(Book.project, Book.source, Book.status)).all()
    by_project: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
    for project, collection, status, count in rows:
        if project is None:          # excluded by the IN filter; narrows the nullable column
            continue
        by_project[project][collection][status] = count
    return [_project_out(svc, slug, by_project.get(slug, {})) for slug in svc.config.projects]


def _project_out(svc: Services, slug: str, counts: dict[str, dict[str, int]]) -> dto.ProjectOut:
    cfg = svc.config.projects[slug]
    keys = list(cfg.collections) + sorted(set(counts) - set(cfg.collections))
    totals: dict[str, int] = defaultdict(int)
    for per_status in counts.values():
        for status, n in per_status.items():
            totals[status] += n
    return dto.ProjectOut(slug=slug, name=cfg.name, auto_publish=cfg.auto_publish,
                          outputs=list(cfg.outputs),
                          collections={k: cfg.collections.get(k, k) for k in keys},
                          collection_counts={k: dict(counts.get(k, {})) for k in keys},
                          counts=dict(totals))


def project_config(svc: Services, slug: str) -> dto.ProjectConfigOut:
    """The read-only preset; secrets never leave the server (only the env var's name)."""
    cfg = svc.project(slug)
    if cfg is None:
        raise NotFound(f"unknown project {slug!r}")
    publish = {"target": cfg.publish.target, "base_url": cfg.publish.base_url,
               "catalog": cfg.publish.catalog, "files_base_url": cfg.publish.files_base_url,
               "api_key_env": cfg.publish.api_key_env}
    return dto.ProjectConfigOut(
        slug=cfg.slug, name=cfg.name, book_pattern=cfg.book_pattern, frames_glob=cfg.frames_glob,
        item_id=cfg.item_id, language=cfg.language, ocr_language=cfg.ocr_language,
        layout_detectors=list(cfg.layout_detectors), formula_recognizer=cfg.formula_recognizer,
        outputs=list(cfg.outputs), auto_publish=cfg.auto_publish, quality=asdict(cfg.quality),
        publish=publish, metadata={k: v for k, v in cfg.metadata.items() if k != "columns"})


def import_collection(svc: Services, project: str, collection: str | None) -> dto.TaskOut:
    """Enqueue ``studio.discover``; the worker upserts books and announces them."""
    cfg = svc.project(project)
    if cfg is None:
        raise NotFound(f"unknown project {project!r}")
    if collection is not None and cfg.collections and collection not in cfg.collections:
        raise Invalid(f"unknown collection {collection!r} in project {project!r}")
    task_id = svc.queue.enqueue_discover(project, collection)
    svc.bus.publish(Event(EventType.IMPORT_STARTED, project, data={"collection": collection,
                                                                     "task_id": task_id}))
    return dto.TaskOut(task_id=task_id)


# ---------------------------------------------------------------- listing
def list_books(svc: Services, query: dto.BookQuery) -> dto.BookPage:
    where = _filters(query)
    with svc.uow() as uow:
        total = uow.session.scalar(select(func.count()).select_from(Book).where(*where)) or 0
        books = uow.session.scalars(
            select(Book).where(*where).order_by(_order(query.sort), Book.id)
            .limit(query.limit).offset(query.offset)).all()
        disputed = disputed_counts(uow.session, [b.id for b in books])
        return dto.BookPage(items=[dto.book_out(b, disputed.get(b.id, 0)) for b in books],
                            total=total)


def disputed_counts(session: Session, book_ids: list[int]) -> dict[int, int]:
    """book id -> open disputed regions, in one grouped query for a whole page of rows.

    The queue and the table show it so an operator never approves (and so
    publishes) a book whose disputes nobody looked at.
    """
    if not book_ids:
        return {}
    rows = session.execute(
        select(Page.book_pk, func.count(Region.id)).join(Region, Region.page_pk == Page.id)
        .where(Page.book_pk.in_(book_ids), Region.status == RegionStatus.DISPUTED)
        .group_by(Page.book_pk)).all()
    return {book_pk: int(n) for book_pk, n in rows}


def _filters(q: dto.BookQuery) -> list[ColumnElement[bool]]:
    where: list[ColumnElement[bool]] = []
    if q.project:
        where.append(Book.project == q.project)
    if q.collection:
        where.append(Book.source == q.collection)
    if q.status:
        where.append(Book.status == q.status)
    if q.q:
        like = f"%{q.q.strip()}%"
        where.append(or_(Book.title.ilike(like), Book.item_id.ilike(like), Book.book_id.ilike(like)))
    if q.flagged is not None:
        passed = Book.quality["passed"].as_boolean()
        where.append(passed.is_(False) if q.flagged else or_(passed.is_(None), passed.is_(True)))
    return where


def _order(sort: str) -> UnaryExpression[Any]:
    column = _SORTS.get(sort.lstrip("-"))
    if column is None:
        raise Invalid(f"unknown sort {sort!r} (one of {', '.join(_SORTS)}, '-' for descending)")
    return (column.desc() if sort.startswith("-") else column.asc()).nulls_last()


# ---------------------------------------------------------------- detail
def get_book(svc: Services, book_id: int) -> dto.BookDetail:
    with svc.uow() as uow:
        book = load_book(uow.session, book_id)
        return _detail(svc, uow.session, book)


def _detail(svc: Services, session: Session, book: Book) -> dto.BookDetail:
    store = svc.artifacts_for(book.project) if book.project else None
    pubs = [dto.publication_out(p) for p in book.publications]
    return dto.BookDetail(
        book=dto.book_out(book, disputed_counts(session, [book.id]).get(book.id, 0)),
        item_metadata=item_metadata_of(book).as_dict(),
        quality=dict(book.quality or {}),
        runs=[dto.run_out(r) for r in reversed(book.runs[-10:])],
        publication=pubs[-1] if pubs else None, publications=pubs,
        reviews=[dto.review_out(r) for r in book.reviews],
        notes=[dto.note_out(n) for n in book.notes if n.page_pk is None and n.region_pk is None],
        artifacts=artifact_list(book, store), paths=_paths(book, store),
        pages=_page_counts(session, book))


def item_metadata_of(book: Book) -> ItemMetadata:
    """``books.item_id`` always wins (permanent); a classic book falls back to its slug."""
    return ItemMetadata.for_item(book.item_id or book.slug, book.item_metadata, book.title)


def artifact_path(book: Book, kind: str, store: ArtifactStore | None) -> str | None:
    """Recorded path first (the DB is the source of truth), else where the store would put it."""
    recorded = (book.artifacts or {}).get(kind)
    if recorded:
        return str(recorded)
    if store is not None and book.item_id and kind in IA_KINDS:
        return str(store.path(book.item_id, kind))
    return None


def artifact_list(book: Book, store: ArtifactStore | None) -> list[dto.ArtifactOut]:
    kinds = list(IA_KINDS) + sorted(set(book.artifacts or {}) - set(IA_KINDS))
    out = []
    for kind in kinds:
        ref = dto.file_ref(artifact_path(book, kind, store))
        size = Path(ref.path).stat().st_size if ref.exists and ref.path else None
        out.append(dto.ArtifactOut(kind=kind, path=ref.path, exists=ref.exists, size=size,
                                   url=f"{dto.API_PREFIX}/books/{book.id}/files/{kind}"))
    return out


def artifact_file(svc: Services, book_id: int, kind: str) -> Path:
    """The file behind ``GET /books/{id}/files/{kind}``: a known kind, recorded or IA-placed."""
    with svc.uow() as uow:
        book = load_book(uow.session, book_id)
        if kind not in ARTIFACT_KINDS and kind not in (book.artifacts or {}):
            raise NotFound(f"unknown artifact kind {kind!r}")
        store = svc.artifacts_for(book.project) if book.project else None
        path = artifact_path(book, kind, store)
    if not path or not Path(path).is_file():
        raise NotFound(f"book {book_id} has no {kind} file")
    return Path(path)


def _paths(book: Book, store: ArtifactStore | None) -> dict[str, str | None]:
    item_dir = store.item_dir(book.item_id) if store is not None and book.item_id else None
    return {"source_dir": book.source_dir, "work_dir": book.work_dir,
            "output_dir": book.output_dir, "item_dir": str(item_dir) if item_dir else None}


def _page_counts(session: Session, book: Book) -> dto.PageCounts:
    total = session.scalar(select(func.count()).select_from(Page).where(Page.book_pk == book.id))
    access = session.scalar(select(func.count()).select_from(Page)
                            .where(Page.book_pk == book.id, Page.add_to_access.is_(True)))
    disputed = session.scalar(
        select(func.count(func.distinct(Region.page_pk))).join(Page)
        .where(Page.book_pk == book.id, Region.status == RegionStatus.DISPUTED))
    return dto.PageCounts(total=total or 0, access=access or 0, disputed=disputed or 0)


# ---------------------------------------------------------------- metadata
def update_item_metadata(svc: Services, book_id: int, patch: dict[str, Any]) -> dict[str, Any]:
    """Validate and store a partial IA metadata update; the title column follows it."""
    with svc.uow() as uow:
        book = load_book(uow.session, book_id)
        meta = apply_patch(item_metadata_of(book).as_dict(), patch)
        book.item_metadata = meta.as_dict()
        book.title = meta.title or book.title
        return meta.as_dict()
