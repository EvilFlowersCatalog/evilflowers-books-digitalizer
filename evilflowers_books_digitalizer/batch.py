"""Low-storage batch processing.

One call of :func:`process_book` digitizes one book end to end and cleans up
after itself: the staged TIFFs (symlinks or copies), the preprocessed pages and
the raw (pre-OCR) PDF are removed; only the final PDF, text sidecar and cover
remain.

Designed to run under ``concurrent.futures.ProcessPoolExecutor`` (notebook 05)
or as a Prefect task (``orchestration.flows``) — OCRmyPDF / recode_pdf are not
thread-safe, and separate processes also parallelize the imaging. Everything is
constructed *inside* the worker from plain arguments, so nothing non-picklable
crosses the process boundary.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

QUIET_LOGGERS = ("httpx", "httpcore", "ocrmypdf", "fontTools", "pikepdf")


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def expected_pdf(output_dir: Path, source_key: str, book_id: str) -> Path:
    """Final PDF location for a book (mirrors ``BookContext.slug``)."""
    return output_dir / source_key / f"{source_key}_{book_id}.pdf"


def process_book(
    source_key: str,
    book_id: str,
    jobs: int | None = None,
    min_free_gb: float = 40.0,
    disk_wait_minutes: float = 30.0,
    keep_cache: bool = False,
    config_path: str | None = None,
) -> dict:
    """Digitize one book; returns a plain-dict report row.

    ``jobs`` caps OCR/recode internal parallelism — with N books in flight,
    pass roughly ``cpu_count // N``. ``min_free_gb`` guards the disk: the worker
    waits (up to ``disk_wait_minutes``) for other workers' cleanups to free
    space before staging. ``config_path`` overrides ``configs/pipeline.toml``.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in QUIET_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.ERROR)

    # heavy imports stay inside the worker so the parent stays light
    from evilflowers_books_digitalizer import BookContext, LocalCache, build_source
    from evilflowers_books_digitalizer.pipeline.factory import build_pipeline
    from evilflowers_books_digitalizer.runtime import build_catalog, load_runtime

    rt = load_runtime(config_path)
    cache = LocalCache(rt.cache_dir)
    row: dict = {"source": source_key, "book_id": book_id, "status": "ok"}
    started = time.perf_counter()

    pdf = expected_pdf(rt.output_dir, source_key, book_id)
    if pdf.exists():
        return {**row, "status": "skipped", "pdf": str(pdf), "pdf_mb": pdf.stat().st_size / 1e6}

    deadline = time.monotonic() + disk_wait_minutes * 60
    while free_gb(rt.cache_dir.parent) < min_free_gb:
        if time.monotonic() > deadline:
            return {**row, "status": "error", "error": f"low disk (<{min_free_gb} GB) — timed out"}
        logger.warning("%s_%s: waiting for disk space", source_key, book_id)
        time.sleep(30)

    ctx = BookContext(
        source=source_key,
        book_id=book_id,
        work_dir=cache.book_dir(source_key, book_id),
        output_dir=rt.output_dir / source_key,
    )
    try:
        source = build_source(rt.source, source_key)
        catalog = build_catalog(rt.metadata)
        pipeline = build_pipeline(source, cache, jobs=jobs, config=rt.config, catalog=catalog)
        ctx = pipeline.run(ctx)
        cover = ctx.artifacts.get("cover")
        row.update(
            pdf=str(ctx.artifacts["pdf"]),
            pdf_mb=ctx.artifacts["pdf"].stat().st_size / 1e6,
            n_frames=ctx.metadata.get("n_frames"),
            n_pages=ctx.metadata.get("n_pages"),
            language=ctx.metadata.get("ocr_language"),
            ocr_chars=ctx.metadata.get("n_text_chars"),
            title=ctx.metadata.get("title"),
            catalog_matched=ctx.metadata.get("catalog_matched"),
            cover=str(cover) if cover else None,
        )
    except Exception as exc:  # noqa: BLE001 — isolate per-book failures
        row.update(status="error", error=f"{type(exc).__name__}: {exc}"[:500])
    finally:
        # low-storage mode: drop everything except the final PDF + sidecar + cover
        if not keep_cache:
            shutil.rmtree(cache.book_dir(source_key, book_id), ignore_errors=True)
        raw_pdf = ctx.artifacts.get("raw_pdf")
        if raw_pdf is not None:
            Path(raw_pdf).unlink(missing_ok=True)

    row["minutes"] = round((time.perf_counter() - started) / 60, 2)
    return row
