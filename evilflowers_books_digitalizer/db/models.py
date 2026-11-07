"""Database schema: one row per book, one row per produced page.

Deliberately small. Files stay on disk; the database records *state* (status,
current step, timings, errors) and *where everything is* (source frames,
work dir, outputs, previews) so the web UI can show progress and compare an
original TIFF with the page it became.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive UTC (SQLite stores no zone; keep every timestamp comparable)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    # float pinned to Float: SQLAlchemy 2.1 maps it to Double, which would drift from the migrations
    type_annotation_map = {dict[str, Any]: JSON, float: Float}


#: Book.status values (classic batch runner); the studio adds domain.BookStatus values
STATUSES = ("queued", "running", "ok", "error")


class Book(Base):
    __tablename__ = "books"
    __table_args__ = (UniqueConstraint("source", "book_id", name="uq_books_source_book"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    book_id: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(300), unique=True)
    title: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    step: Mapped[str | None] = mapped_column(String(64))
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    n_steps: Mapped[int] = mapped_column(Integer, default=0)
    n_frames: Mapped[int | None]
    n_pages: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    #: where the original frames live (on the mount) / the scratch dir / the outputs
    source_dir: Mapped[str | None] = mapped_column(String(1024))
    work_dir: Mapped[str | None] = mapped_column(String(1024))
    output_dir: Mapped[str | None] = mapped_column(String(1024))
    #: name -> absolute path (pdf_distribution, pdf_archival, epub, text, cover, ...)
    artifacts: Mapped[dict[str, Any]] = mapped_column(default=dict)
    #: free-form numbers worth showing (sizes, minutes, reflow report, language)
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)
    queued_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    # ---- studio (0002) ---------------------------------------------------------
    #: project slug from the studio config; NULL for books written by the classic batch runner
    project: Mapped[str | None] = mapped_column(String(64), index=True)
    #: IA-compatible item identifier (studio.metadata.ItemMetadata.identifier)
    item_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    #: ItemMetadata.as_dict() — IA _meta.xml semantics
    item_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)
    #: QualityReport.as_dict() of the last gate
    quality: Mapped[dict[str, Any]] = mapped_column(default=dict)
    quality_score: Mapped[float | None] = mapped_column(index=True)

    pages: Mapped[list[Page]] = relationship(
        back_populates="book", order_by="Page.index", cascade="all, delete-orphan")
    runs: Mapped[list[Run]] = relationship(
        back_populates="book", order_by="Run.id", cascade="all, delete-orphan")
    notes: Mapped[list[Note]] = relationship(
        back_populates="book", order_by="Note.id", cascade="all, delete-orphan")
    reviews: Mapped[list[Review]] = relationship(
        back_populates="book", order_by="Review.id", cascade="all, delete-orphan")
    publications: Mapped[list[Publication]] = relationship(
        back_populates="book", order_by="Publication.id", cascade="all, delete-orphan")

    @property
    def progress(self) -> float:
        """0..1 — finished books are 1, running books the fraction of *completed* steps
        (``step_index`` is the 1-based number of the step currently running)."""
        if self.status in ("ok", "error"):
            return 1.0
        if self.status == "running" and self.n_steps:
            return min(1.0, max(0, self.step_index - 1) / self.n_steps)
        return 0.0


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("book_pk", "index", name="uq_pages_book_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    book_pk: Mapped[int] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"), index=True)
    index: Mapped[int]                                     # 0-based, = PDF page index
    source_path: Mapped[str | None] = mapped_column(String(1024))   # original TIFF frame
    side: Mapped[int | None]                               # spread half (0 left, 1 right)
    n_sides: Mapped[int | None]
    x0: Mapped[int | None]                                 # half's x-range in the frame
    x1: Mapped[int | None]
    preview_path: Mapped[str | None] = mapped_column(String(1024))  # cleaned page (WebP)
    # ---- studio (0002): IA scandata leaf semantics + processing state -------------
    page_type: Mapped[str] = mapped_column(String(32), default="Normal")   # metadata.PageType
    hand_side: Mapped[str | None] = mapped_column(String(8))              # LEFT | RIGHT
    add_to_access: Mapped[bool] = mapped_column(default=True)
    page_number: Mapped[str | None] = mapped_column(String(32))           # printed number
    crop_box: Mapped[list[int] | None] = mapped_column(JSON)              # [x, y, w, h] in the frame
    rotate_degree: Mapped[int] = mapped_column(Integer, default=0)
    skew_angle: Mapped[float | None]
    clean_path: Mapped[str | None] = mapped_column(String(1024))          # cleaned page image
    dzi_path: Mapped[str | None] = mapped_column(String(1024))            # deep-zoom descriptor
    width: Mapped[int | None]                                             # cleaned page px
    height: Mapped[int | None]
    dpi: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(16), default="pending")    # domain.PageStatus
    version: Mapped[int] = mapped_column(Integer, default=0)              # bumped on every re-run
    ocr_text: Mapped[str | None] = mapped_column(Text)
    ocr_conf: Mapped[float | None]
    hocr_path: Mapped[str | None] = mapped_column(String(1024))
    override: Mapped[dict[str, Any]] = mapped_column(default=dict)        # domain.PageOverride
    #: operator-only scandata fields: {"book_start": bool, "toc_title": str, "toc_level": int}
    leaf_extra: Mapped[dict[str, Any]] = mapped_column(default=dict)

    book: Mapped[Book] = relationship(back_populates="pages")
    regions: Mapped[list[Region]] = relationship(
        back_populates="page", order_by="Region.id", cascade="all, delete-orphan")


class Region(Base):
    """A rectangle on a cleaned page with a meaning (domain.RegionKind) and a decision."""

    __tablename__ = "regions"

    id: Mapped[int] = mapped_column(primary_key=True)
    page_pk: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    x0: Mapped[int]
    y0: Mapped[int]
    x1: Mapped[int]
    y1: Mapped[int]
    source: Mapped[str] = mapped_column(String(16))         # domain.RegionSource
    confidence: Mapped[float] = mapped_column(default=1.0)
    status: Mapped[str] = mapped_column(String(16), index=True)   # domain.RegionStatus
    action: Mapped[str] = mapped_column(String(16))         # domain.RegionAction
    text: Mapped[str | None] = mapped_column(Text)          # OCR text or LaTeX
    alternatives: Mapped[list[str] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(String(500))
    author: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    page: Mapped[Page] = relationship(back_populates="regions")


class Run(Base):
    """One scheduled piece of work on a book (domain.RunKind / RunStatus)."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_pk: Mapped[int] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    interactive: Mapped[bool] = mapped_column(default=False)
    pages: Mapped[list[int] | None] = mapped_column(JSON)    # None = whole book
    stage: Mapped[str | None] = mapped_column(String(32))
    done: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    log: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)   # last ~200 lines
    task_id: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    # ---- 0003: liveness (studio.workers.recovery) ----------------------------------
    #: last sign of life of a task working on this run (bumped while a task body runs)
    heartbeat_at: Mapped[datetime | None]
    #: when the run's canvas was handed to the broker; NULL while running = never dispatched
    dispatched_at: Mapped[datetime | None]

    book: Mapped[Book] = relationship(back_populates="runs")


class Note(Base):
    """Operator note on a book, a page or a region."""

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_pk: Mapped[int] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"), index=True)
    page_pk: Mapped[int | None] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"))
    region_pk: Mapped[int | None] = mapped_column(ForeignKey("regions.id", ondelete="CASCADE"))
    author: Mapped[str | None] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    book: Mapped[Book] = relationship(back_populates="notes")


class Review(Base):
    """An operator's decision on a book (domain.ReviewDecision)."""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_pk: Mapped[int] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"), index=True)
    decision: Mapped[str] = mapped_column(String(16))
    author: Mapped[str | None] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    book: Mapped[Book] = relationship(back_populates="reviews")


class Publication(Base):
    """A book's presence in a catalog: entry id + version; republishing appends a row."""

    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_pk: Mapped[int] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"), index=True)
    target: Mapped[str] = mapped_column(String(32))          # adapter name, e.g. evilflowers
    entry_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    url: Mapped[str | None] = mapped_column(String(1024))
    files: Mapped[dict[str, Any]] = mapped_column(default=dict)       # format -> {path, url, acquisition}
    author: Mapped[str | None] = mapped_column(String(100))
    published_at: Mapped[datetime] = mapped_column(default=utcnow)

    book: Mapped[Book] = relationship(back_populates="publications")
