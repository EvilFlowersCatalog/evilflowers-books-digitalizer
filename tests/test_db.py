"""Processing database: migrations, recorder lifecycle, import of existing outputs."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pikepdf
from sqlalchemy import inspect, select

from evilflowers_books_digitalizer.db import Book, Recorder, session_scope, upgrade
from evilflowers_books_digitalizer.db.importer import import_outputs
from evilflowers_books_digitalizer.db.models import Base
from evilflowers_books_digitalizer.db.store import current_revision, get_engine


def _rt(tmp_path: Path, **db):
    return types.SimpleNamespace(config={"database": db} if db else {}, output_dir=tmp_path / "out")


def _url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


def _pdf(path: Path, pages: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(200, 300))
    pdf.save(path)
    return path


def test_migrations_create_exactly_the_model_schema(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    url = _url(tmp_path)
    upgrade(url)
    upgrade(url)  # idempotent
    assert current_revision(url) == "0003"
    with get_engine(url).connect() as conn:
        assert set(inspect(conn).get_table_names()) >= {
            "books", "pages", "alembic_version", "regions", "runs", "notes", "reviews", "publications"}
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_default_url_lives_in_output_dir(tmp_path):
    rec = Recorder(_rt(tmp_path))
    assert rec.url == f"sqlite:///{(tmp_path / 'out' / 'digitizer.db').resolve()}"


def test_recorder_lifecycle(tmp_path):
    rec = Recorder(_rt(tmp_path, url=_url(tmp_path)))
    rec.migrate()
    rec.queued("fei", ["A", "B"])
    rec.started("fei", "A", work_dir=tmp_path / "w", output_dir=tmp_path / "o")
    rec.step("fei", "A", 2, 10, "ocr")
    with session_scope(rec.url) as s:
        a = s.scalar(select(Book).where(Book.book_id == "A"))
        assert (a.status, a.step, a.step_index, a.n_steps) == ("running", "ocr", 3, 10)
        assert a.progress == 0.2       # step 3 of 10 is running -> 2 completed

    previews = tmp_path / "o" / "fei_A.pages"
    previews.mkdir(parents=True)
    (previews / "0001.webp").write_bytes(b"x")
    (previews / "pages.json").write_text(json.dumps([
        {"frame": "/mnt/scans/f1.tif", "side": 0, "n_sides": 2, "x0": 0, "x1": 900},
        {"frame": "/mnt/scans/f1.tif", "side": 1, "n_sides": 2, "x0": 900, "x1": 1800},
    ]))
    rec.finished({"source": "fei", "book_id": "A", "status": "ok", "n_pages": 2, "n_frames": 1,
                  "pdf": "/o/a.pdf", "previews": str(previews), "epub": "/o/a.epub",
                  "outputs": {"archival": {"path": "/o/a.archival.pdf"}}, "minutes": 1.5,
                  "source_dir": "/mnt/scans"})
    rec.finished({"source": "fei", "book_id": "B", "status": "error", "error": "boom"})
    with session_scope(rec.url) as s:
        a = s.scalar(select(Book).where(Book.book_id == "A"))
        b = s.scalar(select(Book).where(Book.book_id == "B"))
        assert a.status == "ok" and a.source_dir == "/mnt/scans" and a.work_dir is None
        assert a.artifacts["epub"] == "/o/a.epub" and a.artifacts["pdf_archival"] == "/o/a.archival.pdf"
        assert [(p.index, p.side, p.x1) for p in a.pages] == [(0, 0, 900), (1, 1, 1800)]
        assert a.pages[0].preview_path.endswith("0001.webp") and a.pages[1].preview_path is None
        assert (b.status, b.error, b.progress) == ("error", "boom", 1.0)

    # re-run: already-produced books are reported "skipped" and stay ok
    rec.queued("fei", ["A"])
    rec.finished({"source": "fei", "book_id": "A", "status": "skipped", "pdf": "/o/a.pdf"})
    with session_scope(rec.url) as s:
        a = s.scalar(select(Book).where(Book.book_id == "A"))
        assert a.status == "ok" and len(a.pages) == 2


def test_recorder_is_fail_safe(tmp_path):
    rec = Recorder(_rt(tmp_path, url="postgresql+nosuchdriver://nowhere/db"))
    rec.migrate()                       # logs, disables itself
    rec.started("fei", "A", work_dir=tmp_path, output_dir=tmp_path)
    rec.finished({"source": "fei", "book_id": "A", "status": "ok"})   # no exception
    assert rec.enabled is False


def test_import_outputs_relocates_and_scans(tmp_path):
    out = tmp_path / "output"
    pdf = _pdf(out / "fei" / "fei_BOOK1.pdf", pages=4)
    (out / "fei" / "fei_BOOK1.txt").write_text("a\fb\fc\fd")
    (out / "fei" / "fei_BOOK1.epub").write_bytes(b"epub")
    _pdf(out / "svf" / "svf_X.distribution.pdf", pages=2)
    _pdf(out / "svf" / "svf_X.archival.pdf", pages=2)
    _pdf(out / "svf" / "svf_X_raw.pdf", pages=2)                 # never a book
    # report row pointing at a tree that moved (output_local/ -> output/)
    (out / "batch_report_fei.jsonl").write_text(
        json.dumps({"source": "fei", "book_id": "BOOK1", "status": "ok", "n_pages": 4,
                    "pdf": "/gone/output_local/fei/fei_BOOK1.pdf", "minutes": 3}) + "\n" +
        json.dumps({"source": "fei", "book_id": "BAD", "status": "error", "error": "x"}) + "\n")
    url = _url(tmp_path)
    upgrade(url)
    counts = import_outputs(url, out, ["fei", "svf"])
    assert counts == {"books": 3, "with_pages": 2, "with_originals": 0}
    with session_scope(url) as s:
        books = {b.slug: b for b in s.scalars(select(Book))}
        assert books["fei_BOOK1"].artifacts["pdf"] == str(pdf)            # relocated
        assert books["fei_BOOK1"].artifacts["epub"].endswith("fei_BOOK1.epub")
        assert len(books["fei_BOOK1"].pages) == 4
        assert books["svf_X"].artifacts["pdf_archival"].endswith("svf_X.archival.pdf")
        assert len(books["svf_X"].pages) == 2                             # from the PDF page count
        assert books["fei_BAD"].status == "error"
    assert import_outputs(url, out, ["fei", "svf"])["books"] == 3        # idempotent
