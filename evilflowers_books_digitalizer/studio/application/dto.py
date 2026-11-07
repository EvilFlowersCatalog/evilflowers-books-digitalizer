"""Pydantic v2 DTOs: the JSON shapes of /api/v1 (and the CLI's ``--json`` output).

Inputs validate what an operator sends; outputs are built from ORM rows by the
``*_out`` functions below, so the mapping lives in one place and the API never
serialises ORM objects directly (no lazy-load surprises, stable field names).

Links (``thumb_url``, ``url``) are relative to :data:`API_PREFIX` so the SPA
never has to know the routing table; they are the only presentational detail
the application layer carries.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from evilflowers_books_digitalizer.studio.application.uow import (
    Book,
    Note,
    Page,
    Publication,
    Region,
    Review,
    Run,
)
from evilflowers_books_digitalizer.studio.domain import (
    BBox,
    BookStatus,
    RegionAction,
    RegionKind,
    RegionStatus,
    ReviewDecision,
    RunKind,
)
from evilflowers_books_digitalizer.studio.metadata import PageType

API_PREFIX = "/api/v1"
#: how many log lines a RunOut carries (the row keeps ~200)
LOG_TAIL = 50
THUMB_WIDTH = 240


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- inputs
class ImportIn(_In):
    collection: str | None = None


class RunIn(_In):
    kind: RunKind
    pages: list[int] | None = None
    interactive: bool | None = None


class PagePatch(_In):
    """Unset fields are left alone; ``page_number: null`` clears the printed number."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    page_type: PageType | None = None
    page_number: str | None = None
    add_to_access: bool | None = Field(
        None, validation_alias=AliasChoices("add_to_access", "include_in_access"))
    override: dict[str, Any] | None = None
    leaf_extra: dict[str, Any] | None = None


def _check_bbox(value: list[int] | None) -> list[int] | None:
    if value is None:
        return None
    if len(value) != 4:
        raise ValueError("bbox must be [x0, y0, x1, y1]")
    BBox(*value)   # raises ValueError on an empty box
    return value


class RegionIn(_In):
    kind: RegionKind
    bbox: list[int]
    action: RegionAction | None = None
    text: str | None = None

    _bbox = field_validator("bbox")(_check_bbox)


class RegionPatch(_In):
    kind: RegionKind | None = None
    bbox: list[int] | None = None
    status: RegionStatus | None = None
    action: RegionAction | None = None
    text: str | None = None

    _bbox = field_validator("bbox")(_check_bbox)


class NoteIn(_In):
    book_id: int | None = None
    page_id: int | None = None
    region_id: int | None = None
    text: str = Field(min_length=1, max_length=10_000)


class ReviewIn(_In):
    decision: ReviewDecision
    note: str | None = None


class BookQuery(_In):
    project: str | None = None
    collection: str | None = None
    status: BookStatus | None = None
    q: str | None = None
    flagged: bool | None = None
    limit: int = Field(50, ge=1, le=500)
    offset: int = Field(0, ge=0)
    sort: str = "id"


# ---------------------------------------------------------------- outputs
class FileRef(BaseModel):
    path: str | None
    exists: bool


def file_ref(path: str | Path | None) -> FileRef:
    if not path:
        return FileRef(path=None, exists=False)
    return FileRef(path=str(path), exists=Path(path).exists())


class RunOut(BaseModel):
    id: int
    book_id: int
    kind: str
    status: str
    interactive: bool
    pages: list[int] | None
    stage: str | None
    done: int
    total: int
    error: str | None
    log: list[dict[str, Any]]
    task_id: str | None
    created_by: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def run_out(run: Run) -> RunOut:
    return RunOut(id=run.id, book_id=run.book_pk, kind=run.kind, status=run.status,
                  interactive=bool(run.interactive), pages=run.pages, stage=run.stage,
                  done=run.done or 0, total=run.total or 0, error=run.error,
                  log=list(run.log or [])[-LOG_TAIL:], task_id=run.task_id,
                  created_by=run.created_by, created_at=run.created_at,
                  started_at=run.started_at, finished_at=run.finished_at)


class BookOut(BaseModel):
    id: int
    project: str | None
    collection: str
    external_id: str
    slug: str
    item_id: str | None
    title: str | None
    status: str
    quality_score: float | None
    flags: list[str]
    flagged: bool                  # the gate failed: review status or quality flags present
    n_pages: int | None
    n_frames: int | None
    updated_at: datetime
    latest_run: RunOut | None
    step: str | None               # stage of the active run (dashboard bars)
    progress: float                # 0..1 of the active run; 0 when idle
    disputed: int = 0              # open (disputed) regions now, not at the last gate


_ACTIVE_RUN = ("queued", "running")


def book_out(book: Book, disputed: int = 0) -> BookOut:
    runs = book.runs
    latest = runs[-1] if runs else None
    active = latest if latest is not None and latest.status in _ACTIVE_RUN else None
    flags = list((book.quality or {}).get("flags") or [])
    return BookOut(id=book.id, project=book.project, collection=book.source,
                   external_id=book.book_id, slug=book.slug, item_id=book.item_id,
                   title=book.title, status=book.status, quality_score=book.quality_score,
                   flags=flags, flagged=book.status == BookStatus.REVIEW or bool(flags),
                   n_pages=book.n_pages if book.n_pages is not None else (len(book.pages) or None),
                   n_frames=book.n_frames, updated_at=book.updated_at,
                   latest_run=run_out(latest) if latest else None,
                   step=active.stage if active else None,
                   progress=(active.done / active.total) if active and active.total else 0.0,
                   disputed=disputed)


class BookPage(BaseModel):
    items: list[BookOut]
    total: int


class ProjectOut(BaseModel):
    slug: str
    name: str
    auto_publish: str
    outputs: list[str]
    collections: dict[str, str]                       # key -> human name, config order
    collection_counts: dict[str, dict[str, int]]      # key -> status -> books
    counts: dict[str, int]                            # status -> books (whole project)


class ProjectConfigOut(BaseModel):
    """Read-only project preset for the Settings screen (no secrets: only the key's
    env variable name is shown, never its value)."""

    slug: str
    name: str
    book_pattern: str
    frames_glob: str
    item_id: str
    language: str
    ocr_language: str
    layout_detectors: list[str]
    formula_recognizer: str
    outputs: list[str]
    auto_publish: str
    quality: dict[str, Any]
    publish: dict[str, Any]
    metadata: dict[str, Any]


class ArtifactOut(BaseModel):
    kind: str
    path: str | None
    exists: bool
    size: int | None
    url: str


class PublicationOut(BaseModel):
    id: int
    target: str
    entry_id: str
    version: int
    url: str | None
    files: dict[str, Any]
    author: str | None
    published_at: datetime


def publication_out(p: Publication) -> PublicationOut:
    return PublicationOut(id=p.id, target=p.target, entry_id=p.entry_id, version=p.version,
                          url=p.url, files=dict(p.files or {}), author=p.author,
                          published_at=p.published_at)


class ReviewOut(BaseModel):
    id: int
    book_id: int
    decision: str
    author: str | None
    note: str | None
    created_at: datetime
    runs: list[RunOut] = []      # runs the decision enqueued (render + publish on approval)


def review_out(r: Review, runs: list[RunOut] | None = None) -> ReviewOut:
    return ReviewOut(id=r.id, book_id=r.book_pk, decision=r.decision, author=r.author,
                     note=r.note, created_at=r.created_at, runs=list(runs or []))


class NoteOut(BaseModel):
    id: int
    book_id: int
    page_id: int | None
    region_id: int | None
    author: str | None
    text: str
    created_at: datetime


def note_out(n: Note) -> NoteOut:
    return NoteOut(id=n.id, book_id=n.book_pk, page_id=n.page_pk, region_id=n.region_pk,
                   author=n.author, text=n.text, created_at=n.created_at)


class PageCounts(BaseModel):
    total: int
    access: int
    disputed: int


class BookDetail(BaseModel):
    book: BookOut
    item_metadata: dict[str, Any]          # ItemMetadata.as_dict() (IA _meta.xml semantics)
    quality: dict[str, Any]
    runs: list[RunOut]
    publication: PublicationOut | None
    publications: list[PublicationOut]
    reviews: list[ReviewOut]
    notes: list[NoteOut]
    artifacts: list[ArtifactOut]
    paths: dict[str, str | None]
    pages: PageCounts


class RegionOut(BaseModel):
    id: int
    page_id: int
    kind: str
    bbox: list[int]
    source: str
    confidence: float
    status: str
    action: str
    text: str | None
    alternatives: list[str]
    reason: str | None
    author: str | None
    updated_at: datetime


def region_out(r: Region) -> RegionOut:
    return RegionOut(id=r.id, page_id=r.page_pk, kind=r.kind, bbox=[r.x0, r.y0, r.x1, r.y1],
                     source=r.source, confidence=r.confidence, status=r.status, action=r.action,
                     text=r.text, alternatives=list(r.alternatives or []), reason=r.reason,
                     author=r.author, updated_at=r.updated_at)


class PageOut(BaseModel):
    id: int
    book_id: int
    index: int                     # leaf number (IA leafNum)
    access_index: int | None       # PDF/EPUB page; None = excluded from access formats
    page_type: str
    hand_side: str | None
    page_number: str | None
    add_to_access: bool
    status: str
    version: int
    width: int | None
    height: int | None
    dpi: int | None
    disputed: int
    thumb_url: str


def page_out(page: Page, access_index: int | None) -> PageOut:
    disputed = sum(1 for r in page.regions if r.status == RegionStatus.DISPUTED)
    return PageOut(id=page.id, book_id=page.book_pk, index=page.index, access_index=access_index,
                   page_type=page.page_type, hand_side=page.hand_side,
                   page_number=page.page_number, add_to_access=bool(page.add_to_access),
                   status=page.status, version=page.version or 0, width=page.width,
                   height=page.height, dpi=page.dpi, disputed=disputed,
                   thumb_url=f"{API_PREFIX}/pages/{page.id}/image/clean?w={THUMB_WIDTH}")


class SourceFrame(BaseModel):
    """Where the leaf was cut from: the original TIFF frame and the IA crop box."""

    path: str | None
    exists: bool
    width: int | None              # original frame px (when the plan recorded it)
    height: int | None
    crop_box: list[int] | None     # x, y, w, h (after rotation)
    rotate_degree: int
    skew_angle: float | None
    side: int | None               # spread half: 0 left, 1 right, None whole frame
    n_sides: int | None
    x0: int | None                 # the half's x-range in the frame
    x1: int | None


def source_frame(page: Page) -> SourceFrame:
    extra = page.leaf_extra or {}
    return SourceFrame(path=page.source_path, exists=file_ref(page.source_path).exists,
                       width=extra.get("orig_width"), height=extra.get("orig_height"),
                       crop_box=page.crop_box, rotate_degree=page.rotate_degree or 0,
                       skew_angle=page.skew_angle, side=page.side, n_sides=page.n_sides,
                       x0=page.x0, x1=page.x1)


class PageDetail(BaseModel):
    page: PageOut
    regions: list[RegionOut]
    notes: list[NoteOut]
    override: dict[str, Any]
    leaf_extra: dict[str, Any]
    ocr_text: str | None
    ocr_conf: float | None
    frame: SourceFrame
    prev_id: int | None            # neighbouring leaves (← / → in the page studio)
    next_id: int | None
    files: dict[str, FileRef]
    images: dict[str, str]


class TaskOut(BaseModel):
    task_id: str
