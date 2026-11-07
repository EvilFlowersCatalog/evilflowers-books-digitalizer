"""Plain batch runner — no orchestrator.

Runs :func:`batch.process_book` over many books with a process pool, appending
each result to a resumable JSONL report and logging progress. This is all the
orchestration a single-VM, run-to-completion mass digitization needs: deploy by
running it under ``tmux``/``systemd`` (see ``deploy/``), monitor with the
``stats`` CLI and ``tail -f`` on the log.

Resumable: ``process_book`` returns ``skipped`` for any book whose final PDF
already exists, so re-running picks up where it left off.

Concurrency comes from ``[orchestration].max_parallel_books`` (each book in its
own OS process — honours the not-thread-safe OCR/recode tools and bounds memory)
× ``ocr_jobs`` threads inside each book. Keep their product ≤ CPU cores. With
``max_parallel<=1`` the runner is sequential and in-process (notebook-safe).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer import progress
from evilflowers_books_digitalizer.batch import process_book
from evilflowers_books_digitalizer.db import Recorder
from evilflowers_books_digitalizer.runtime import load_runtime
from evilflowers_books_digitalizer.sources import build_source

logger = logging.getLogger(__name__)


def _tally(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = row.get("status", "error")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _append(report: Path, row: dict[str, Any]) -> None:
    with report.open("a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


#: called in the parent process with each finished book's row (may add keys to it)
OnResult = Callable[[dict[str, Any]], None]


def run_source(
    source_key: str,
    *,
    book_ids: list[str] | None = None,
    limit: int | None = None,
    max_parallel: int | None = None,
    config_path: str | None = None,
    jobs: int | None = None,
    keep_cache: bool = False,
    on_result: OnResult | None = None,
) -> dict[str, Any]:
    """Digitize books of one faculty (all, or ``book_ids`` in order).

    ``on_result(row)`` runs in this process right after each book finishes and
    before the row is written to the report — ``--publish`` hooks in here.
    Returns a counts/rows summary.
    """
    rt = load_runtime(config_path)
    orch = rt.orchestration
    if jobs is None:
        jobs = orch.get("ocr_jobs")
    parallel = max_parallel if max_parallel is not None else orch.get("max_parallel_books", 4)

    ids = book_ids or build_source(rt.source, source_key).list_books()
    if limit:
        ids = ids[:limit]
    report = rt.output_dir / f"batch_report_{source_key}.jsonl"
    report.parent.mkdir(parents=True, exist_ok=True)
    progress.clear(rt.output_dir, source_key)  # drop heartbeats from a prior interrupted run
    db = Recorder(rt)
    db.migrate()                  # once, here — never concurrently in the workers
    db.queued(source_key, ids)    # the web UI's progress bars know the total up front

    logger.info("source %s: %d books, %d in parallel", source_key, len(ids), max(parallel, 1))
    started = time.monotonic()
    rows: list[dict[str, Any]] = []

    def finished(index: int, row: dict[str, Any]) -> None:
        if on_result is not None:
            try:
                on_result(row)
            except Exception as exc:  # noqa: BLE001 — a hook must not stop the batch
                logger.error("result hook failed for %s: %s", row.get("book_id"), exc,
                             exc_info=True)
                row["hook_error"] = f"{type(exc).__name__}: {exc}"[:500]
        rows.append(row)
        _append(report, row)
        _log_progress(index, len(ids), row)

    if parallel <= 1:
        for i, book_id in enumerate(ids, 1):
            finished(i, _run_one(source_key, book_id, jobs, config_path, keep_cache))
    else:
        with ProcessPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(process_book, source_key, b, jobs=jobs,
                                keep_cache=keep_cache, config_path=config_path): b
                for b in ids
            }
            for i, future in enumerate(as_completed(futures), 1):
                book_id = futures[future]
                try:
                    row = future.result()
                except Exception as exc:  # noqa: BLE001 — isolate per-book crashes
                    row = {"source": source_key, "book_id": book_id, "status": "error",
                           "error": f"{type(exc).__name__}: {exc}"[:500]}
                finished(i, row)

    counts = _tally(rows)
    logger.info("source %s done in %.1f min: %s — report %s",
                source_key, (time.monotonic() - started) / 60, counts, report)
    return {"source": source_key, "counts": counts, "rows": rows, "report": str(report)}


def run_corpus(
    sources: list[str] | None = None,
    *,
    limit: int | None = None,
    max_parallel: int | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Digitize every configured faculty sequentially (keeps disk bounded)."""
    rt = load_runtime(config_path)
    keys = sources or rt.source_keys
    results = {k: run_source(k, limit=limit, max_parallel=max_parallel, config_path=config_path)
               for k in keys}
    totals: dict[str, int] = {}
    for res in results.values():
        for status, n in res["counts"].items():
            totals[status] = totals.get(status, 0) + n
    logger.info("corpus done: %s", totals)
    return {"totals": totals, "sources": {k: v["counts"] for k, v in results.items()}}


def _run_one(
    source_key: str,
    book_id: str,
    jobs: int | None,
    config_path: str | None,
    keep_cache: bool = False,
) -> dict:
    """Sequential in-process call (notebook/test-safe path)."""
    try:
        return process_book(source_key, book_id, jobs=jobs, keep_cache=keep_cache,
                            config_path=config_path)
    except Exception as exc:  # noqa: BLE001
        return {"source": source_key, "book_id": book_id, "status": "error",
                "error": f"{type(exc).__name__}: {exc}"[:500]}


def _log_progress(done: int, total: int, row: dict[str, Any]) -> None:
    logger.info("[%d/%d] %s/%s: %s (%s min)", done, total, row.get("source"),
                row.get("book_id"), row.get("status"), row.get("minutes"),
                extra={"source": row.get("source"), "book_id": row.get("book_id"),
                       "status": row.get("status")})
