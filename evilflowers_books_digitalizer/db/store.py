"""Engine/session handling, migrations, and the batch worker's fail-safe recorder.

URL from ``[database] url`` (any SQLAlchemy URL — Postgres works); default is
``sqlite:///<output_dir>/digitizer.db``. SQLite runs in WAL mode with a busy
timeout so the parallel book workers (separate processes, one short write per
step) and the web UI can share it.

Schema changes go through Alembic (``evilflowers_books_digitalizer/db/migrations``),
driven programmatically: ``evilflowers-digitalizer db upgrade``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from evilflowers_books_digitalizer.db.models import Book, Page, utcnow

if TYPE_CHECKING:
    from evilflowers_books_digitalizer.runtime import RuntimeConfig

logger = logging.getLogger(__name__)

MIGRATIONS = Path(__file__).with_name("migrations")
#: written next to the page previews: the page -> original-frame map
PAGES_INDEX = "pages.json"


def database_url(rt: RuntimeConfig) -> str:
    url = rt.config.get("database", {}).get("url")
    return url or f"sqlite:///{(rt.output_dir / 'digitizer.db').resolve()}"


@lru_cache(maxsize=8)
def get_engine(url: str) -> Engine:
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


def get_or_create_book(session: Session, source: str, book_id: str) -> Book:
    book = session.scalar(select(Book).where(Book.source == source, Book.book_id == book_id))
    if book is None:
        book = Book(source=source, book_id=book_id, slug=f"{source}_{book_id}",
                    artifacts={}, stats={})
        session.add(book)
    return book


def replace_pages(session: Session, book: Book, page_map: list[dict[str, Any]],
                  previews: Path | None, n_pages: int | None = None) -> None:
    """Rewrite a book's page rows from the page map (+ preview files when present)."""
    book.pages.clear()
    session.flush()
    count = max(len(page_map), n_pages or 0)
    for i in range(count):
        entry = page_map[i] if i < len(page_map) else {}
        preview = previews / f"{i + 1:04d}.webp" if previews else None
        book.pages.append(Page(
            index=i, source_path=entry.get("frame"), side=entry.get("side"),
            n_sides=entry.get("n_sides"), x0=entry.get("x0"), x1=entry.get("x1"),
            preview_path=str(preview) if preview and preview.exists() else None,
        ))


def read_pages_index(previews: Path | None) -> list[dict[str, Any]]:
    if previews is None or not (previews / PAGES_INDEX).exists():
        return []
    try:
        return list(json.loads((previews / PAGES_INDEX).read_text()))
    except (OSError, ValueError):
        return []


class Recorder:
    """What the batch worker tells the database. Every call is fail-safe.

    A broken or locked database logs a warning and the book carries on — the
    files on disk (and the JSONL report) remain the source of truth.
    """

    def __init__(self, rt: RuntimeConfig) -> None:
        self.enabled = bool(rt.config.get("database", {}).get("enabled", True))
        self.url = database_url(rt)
        self._warned = False

    def _write(self, fn: Any) -> None:
        if not self.enabled:
            return
        try:
            with session_scope(self.url) as session:
                fn(session)
        except Exception as exc:  # noqa: BLE001 — the DB must never fail a book
            if not self._warned:
                logger.warning("database update failed (%s): %s", self.url, exc)
                self._warned = True

    def migrate(self) -> None:
        """Bring the schema to head — once, in the parent, before workers start."""
        if not self.enabled:
            return
        try:
            upgrade(self.url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("database migration failed (%s): %s — continuing without it",
                           self.url, exc)
            self.enabled = False

    def queued(self, source: str, book_ids: list[str]) -> None:
        def fn(s: Session) -> None:
            now = utcnow()
            for book_id in book_ids:
                book = get_or_create_book(s, source, book_id)
                if book.status != "ok":
                    book.status, book.step, book.step_index, book.error = "queued", None, 0, None
                    book.queued_at = now
        self._write(fn)

    def started(self, source: str, book_id: str, *, work_dir: Path, output_dir: Path,
                source_dir: str | None = None) -> None:
        def fn(s: Session) -> None:
            book = get_or_create_book(s, source, book_id)
            book.status, book.step, book.step_index, book.error = "running", "starting", 0, None
            book.started_at, book.finished_at = utcnow(), None
            book.work_dir, book.output_dir = str(work_dir), str(output_dir)
            book.source_dir = source_dir or book.source_dir
        self._write(fn)

    def step(self, source: str, book_id: str, index: int, total: int, name: str) -> None:
        def fn(s: Session) -> None:
            book = get_or_create_book(s, source, book_id)
            book.step, book.step_index, book.n_steps = name, index + 1, total
        self._write(fn)

    def finished(self, row: dict[str, Any]) -> None:
        """Record a ``process_book`` report row (ok / skipped / error)."""
        def fn(s: Session) -> None:
            book = get_or_create_book(s, row["source"], row["book_id"])
            if row["status"] == "skipped":
                # already produced earlier: keep what we know, just make sure it's "ok"
                book.status = "ok"
                book.artifacts = {**(book.artifacts or {}), **artifacts_from_row(row)}
                return
            book.status = "ok" if row["status"] == "ok" else "error"
            book.error = row.get("error")
            book.step = None if book.status == "ok" else book.step
            book.finished_at = utcnow()
            book.title = row.get("title") or book.title
            book.n_frames, book.n_pages = row.get("n_frames"), row.get("n_pages")
            book.source_dir = row.get("source_dir") or book.source_dir
            if not row.get("work_dir"):   # low-storage mode deleted it
                book.work_dir = None
            book.artifacts = {**(book.artifacts or {}), **artifacts_from_row(row)}
            book.stats = stats_from_row(row)
            if book.status == "ok":
                previews = Path(row["previews"]) if row.get("previews") else None
                replace_pages(s, book, read_pages_index(previews), previews, row.get("n_pages"))
        self._write(fn)


#: report-row keys that are paths, mapped to artifact names
_ROW_PATHS = {"pdf": "pdf", "text": "text", "cover": "cover", "manifest": "manifest",
              "epub": "epub", "previews": "previews", "hocr": "hocr", "log": "log",
              "report": "report"}


def artifacts_from_row(row: dict[str, Any]) -> dict[str, str]:
    arts = {name: str(row[key]) for key, name in _ROW_PATHS.items() if row.get(key)}
    for profile, info in (row.get("outputs") or {}).items():
        if isinstance(info, dict) and info.get("path"):
            arts[f"pdf_{profile}"] = str(info["path"])
    return arts


def stats_from_row(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("minutes", "pdf_mb", "language", "ocr_chars", "catalog_matched", "skipped_frames",
            "reflow")
    return {k: row[k] for k in keys if row.get(k) is not None}
