"""Prefect flows for the digitalization corpus.

Three nested flows, all self-hosted-Prefect friendly:

* :func:`digitize_book` — one book, run in its own OS process (honours the
  not-thread-safe OCR/recode constraint and bounds memory growth over a long
  batch). Emits a per-book Markdown artifact for the UI.
* :func:`digitize_source` — every book of one faculty, fanned out under a
  concurrency limit, with a summary table artifact + a resumable JSONL report.
* :func:`digitize_corpus` — all faculties in sequence (keeps disk bounded).

The heavy lifting stays in :func:`batch.process_book`; Prefect only adds
orchestration, retries, concurrency, monitoring and alerting around it.
"""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger, task
from prefect.artifacts import create_markdown_artifact, create_table_artifact
from prefect.futures import wait
from prefect.task_runners import ThreadPoolTaskRunner

from evilflowers_books_digitalizer.batch import process_book
from evilflowers_books_digitalizer.runtime import load_runtime
from evilflowers_books_digitalizer.sources import build_source

_STATUS_EMOJI = {"ok": "✅", "skipped": "⏭️", "error": "❌"}


def _run_book_subprocess(
    source_key: str, book_id: str, jobs: int | None, min_free_gb: float, config_path: str | None
) -> dict:
    """Execute one book in a fresh child process (true isolation)."""
    with ProcessPoolExecutor(max_workers=1) as executor:
        return executor.submit(
            process_book,
            source_key,
            book_id,
            jobs=jobs,
            min_free_gb=min_free_gb,
            config_path=config_path,
        ).result()


def _book_markdown(row: dict) -> str:
    emoji = _STATUS_EMOJI.get(row.get("status", ""), "❔")
    lines = [
        f"# {emoji} {row.get('title') or row['book_id']}",
        "",
        "| field | value |",
        "|---|---|",
    ]
    for key in (
        "source",
        "book_id",
        "status",
        "n_pages",
        "pdf_mb",
        "language",
        "ocr_chars",
        "catalog_matched",
        "minutes",
        "error",
    ):
        if row.get(key) is not None:
            value = round(row[key], 2) if isinstance(row[key], float) else row[key]
            lines.append(f"| {key} | {value} |")
    return "\n".join(lines)


@task(
    name="digitize-book",
    task_run_name="book-{source_key}-{book_id}",
    tags=["book"],
)
def digitize_book(
    source_key: str,
    book_id: str,
    jobs: int | None = None,
    min_free_gb: float = 40.0,
    config_path: str | None = None,
) -> dict:
    """Digitize one book in an isolated process; emit a per-book artifact."""
    logger = get_run_logger()
    row = _run_book_subprocess(source_key, book_id, jobs, min_free_gb, config_path)
    create_markdown_artifact(
        key=f"book-{source_key}-{book_id}".lower().replace("_", "-")[:255],
        markdown=_book_markdown(row),
        description=f"{source_key}/{book_id}",
    )
    logger.info("%s/%s: %s (%s min)", source_key, book_id, row["status"], row.get("minutes"))
    return row


def _summary_table(rows: list[dict]) -> list[dict]:
    return [
        {
            "book": r["book_id"],
            "status": r.get("status"),
            "pages": r.get("n_pages"),
            "MB": round(r["pdf_mb"], 1) if r.get("pdf_mb") else None,
            "min": r.get("minutes"),
            "matched": r.get("catalog_matched"),
            "error": (r.get("error") or "")[:80],
        }
        for r in rows
    ]


def _resolve_max_parallel() -> int:
    """Concurrency cap for the source flow's task runner (read at import).

    Bounds how many ``digitize_book`` task runs execute at once. Resolved from
    the default ``[orchestration].max_parallel_books`` — submitting all books to
    a ThreadPoolTaskRunner of this size naturally queues the excess.
    """
    try:
        return int(load_runtime().orchestration.get("max_parallel_books", 4))
    except Exception:  # noqa: BLE001 — config may be absent at import; use a safe default
        return 4


@flow(
    name="digitize-source",
    task_runner=ThreadPoolTaskRunner(max_workers=_resolve_max_parallel()),  # type: ignore[arg-type]
)
def digitize_source(
    source_key: str,
    limit: int | None = None,
    book_ids: list[str] | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Digitize all books of one faculty under the flow's concurrency limit."""
    logger = get_run_logger()
    rt = load_runtime(config_path)
    orch = rt.orchestration
    jobs = orch.get("ocr_jobs")
    min_free_gb = orch.get("min_free_gb", 40.0)

    ids = book_ids or build_source(rt.source, source_key).list_books()
    if limit:
        ids = ids[:limit]
    logger.info(
        "source %s: %d books, max %d in parallel",
        source_key,
        len(ids),
        orch.get("max_parallel_books", 4),
    )

    # Submit every book; the flow's ThreadPoolTaskRunner (sized above) runs at
    # most `max_parallel_books` at once and queues the rest. Each task runs one
    # book in its own child process.
    futures = [
        digitize_book.submit(
            source_key, bid, jobs=jobs, min_free_gb=min_free_gb, config_path=config_path
        )
        for bid in ids
    ]
    wait(futures)
    results = [f.result(raise_on_failure=False) for f in futures]
    rows = [
        r if isinstance(r, dict) else {"book_id": "?", "status": "error", "error": str(r)}
        for r in results
    ]

    report = _write_report(rt.output_dir, source_key, rows)
    counts = _tally(rows)
    create_table_artifact(
        key=f"summary-{source_key}".lower().replace("_", "-")[:255],
        table=_summary_table(rows),
        description=f"{source_key}: {counts}",
    )
    logger.info("source %s done: %s — report %s", source_key, counts, report)

    # Fail the flow run (-> triggers the failure alert automation) only on a
    # *systematic* failure — a few bad books shouldn't alert-spam a mass run,
    # but a missing binary / unreadable mount (most/all books erroring) should.
    error_ratio = counts.get("error", 0) / max(len(rows), 1)
    threshold = orch.get("fail_on_error_ratio", 0.5)
    if rows and error_ratio >= threshold:
        raise RuntimeError(
            f"{source_key}: {counts.get('error', 0)}/{len(rows)} books failed "
            f"(>= {threshold:.0%}) — likely a systematic problem; see the summary artifact"
        )
    return {"source": source_key, "counts": counts, "report": str(report), "rows": rows}


@flow(name="digitize-corpus")
def digitize_corpus(
    sources: list[str] | None = None,
    limit: int | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Digitize every configured faculty sequentially (disk stays bounded)."""
    logger = get_run_logger()
    rt = load_runtime(config_path)
    keys = sources or rt.source_keys
    results = {}
    for key in keys:
        results[key] = digitize_source(key, limit=limit, config_path=config_path)
    totals: dict[str, int] = {}
    for res in results.values():
        for status, n in res["counts"].items():
            totals[status] = totals.get(status, 0) + n
    logger.info("corpus done: %s", totals)
    return {"totals": totals, "sources": {k: v["counts"] for k, v in results.items()}}


def _tally(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("status", "error")] = counts.get(r.get("status", "error"), 0) + 1
    return counts


def _write_report(output_dir: Path, source_key: str, rows: list[dict]) -> Path:
    """Append rows to a JSONL report (resume parity with notebook 05)."""
    report = output_dir / f"batch_report_{source_key}.jsonl"
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return report
