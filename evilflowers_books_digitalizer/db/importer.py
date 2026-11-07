"""Backfill the database from what is already on disk (``db import``).

Books produced before the database existed are known only from the JSONL
reports and the files in the output tree. This walks both:

* the newest report row per book (status, error, sizes, recorded paths);
* every produced PDF in the output tree, so books without a report row appear too.

Recorded paths that moved (e.g. the old ``output_local/`` tree merged into
``output/``) are re-located by file name under the output dir. Page rows come
from ``<slug>.pages/pages.json`` when the book has previews, else from the PDF's
page count — such legacy books show the PDF and text layers in the viewer, but
no original TIFF.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.db.store import (
    artifacts_from_row,
    get_or_create_book,
    read_pages_index,
    replace_pages,
    session_scope,
    stats_from_row,
)
from evilflowers_books_digitalizer.reporting import latest_per_book, load_reports

logger = logging.getLogger(__name__)

_PDF_SUFFIXES = (".distribution.pdf", ".pdf")
_SKIP = re.compile(r"(\.archival\.pdf|_raw\.pdf|\.reflow\.pdf)$")


def _file_index(root: Path) -> dict[str, Path]:
    """name -> path for every file under the output tree (first hit wins)."""
    index: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() or path.name.endswith(".pages"):
            index.setdefault(path.name, path)
    return index


def _relocate(value: str | None, index: dict[str, Path]) -> str | None:
    if not value:
        return None
    path = Path(value)
    if path.exists():
        return str(path)
    moved = index.get(path.name)
    return str(moved) if moved else None


def _discover(root: Path, sources: list[str]) -> dict[tuple[str, str], Path]:
    """Produced PDFs in the output tree -> {(source, book_id): pdf}."""
    found: dict[tuple[str, str], Path] = {}
    for src in sources:
        for directory in (root / src, root / "distribution" / src):
            if not directory.is_dir():
                continue
            for pdf in sorted(directory.glob(f"{src}_*.pdf")):
                if _SKIP.search(pdf.name):
                    continue
                for suffix in _PDF_SUFFIXES:
                    if pdf.name.endswith(suffix):
                        book_id = pdf.name[len(src) + 1: -len(suffix)]
                        found.setdefault((src, book_id), pdf)
                        break
    return found


def _sidecars(pdf: Path, slug: str) -> dict[str, str]:
    """Everything named after the book next to its distribution PDF (+ archival tree)."""
    d = pdf.parent
    arts: dict[str, str] = {}
    candidates = {
        "text": d / f"{slug}.txt", "cover": d / f"{slug}.cover.jpg", "epub": d / f"{slug}.epub",
        "manifest": d / f"{slug}.entry.json", "previews": d / f"{slug}.pages",
        "pdf_archival": d / f"{slug}.archival.pdf",
        "reflow_epub": d / f"{slug}.reflow.epub", "reflow_pdf": d / f"{slug}.reflow.pdf",
    }
    if d.parent.name == "distribution":   # split layout: archival master in its own tree
        candidates["pdf_archival"] = d.parent.parent / "archival" / d.name / pdf.name
    for name, path in candidates.items():
        if path.exists():
            arts[name] = str(path)
    if "epub" not in arts and "reflow_epub" in arts:
        arts["epub"] = arts.pop("reflow_epub")
    return arts


def _pdf_pages(pdf: str | None) -> int | None:
    if not pdf or not Path(pdf).exists():
        return None
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf)
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:  # noqa: BLE001
        return None


def import_outputs(url: str, output_dir: Path, sources: list[str]) -> dict[str, int]:
    """Upsert every known book; returns counts."""
    index = _file_index(output_dir)
    rows = {(r["source"], r["book_id"]): r for r in latest_per_book(load_reports(output_dir))
            if r.get("source") and r.get("book_id")}
    pdfs = _discover(output_dir, sorted(set(sources) | {s for s, _ in rows}))
    counts = {"books": 0, "with_pages": 0, "with_originals": 0}
    with session_scope(url) as session:
        for key in sorted(set(rows) | set(pdfs)):
            source, book_id = key
            row: dict[str, Any] = dict(rows.get(key) or {"source": source, "book_id": book_id,
                                                         "status": "ok"})
            for k in ("pdf", "text", "cover", "manifest", "epub", "previews"):
                row[k] = _relocate(row.get(k), index)
            for info in (row.get("outputs") or {}).values():
                if isinstance(info, dict):
                    info["path"] = _relocate(info.get("path"), index)
            pdf = Path(row["pdf"]) if row.get("pdf") else pdfs.get(key)
            if row.get("status") == "skipped":
                row["status"] = "ok"
            if row["status"] == "ok" and pdf is None:
                continue  # an "ok" row whose files are gone — nothing to show
            book = get_or_create_book(session, source, book_id)
            arts = artifacts_from_row(row)
            if pdf is not None:
                arts.setdefault("pdf", str(pdf))
                arts.setdefault("pdf_distribution", str(pdf))
                arts = {**_sidecars(pdf, book.slug), **arts}
                book.output_dir = str(pdf.parent)
            book.artifacts = arts
            book.stats = stats_from_row(row)
            book.status = "ok" if row["status"] == "ok" else "error"
            book.error = row.get("error")
            book.title = row.get("title") or book.title
            book.n_frames = row.get("n_frames")
            n_pages = row.get("n_pages") or _pdf_pages(arts.get("pdf"))
            book.n_pages = n_pages
            previews = Path(arts["previews"]) if arts.get("previews") else None
            page_map = read_pages_index(previews)
            if book.status == "ok":
                replace_pages(session, book, page_map, previews, n_pages)
                counts["with_pages"] += bool(n_pages)
                counts["with_originals"] += any(m.get("frame") for m in page_map)
            counts["books"] += 1
    return counts
