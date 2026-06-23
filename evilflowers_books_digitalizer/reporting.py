"""Read and summarize the batch report JSONL files.

Each book run appends a row to ``output/batch_report_<source>.jsonl`` (see
``batch.process_book``). This module turns those rows into headline numbers for
the stats notebook and the ``stats`` CLI command.
Re-runs append, so :func:`latest_per_book` keeps only the newest row per book.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_reports(output_dir: Path, prefix: str = "batch_report") -> list[dict[str, Any]]:
    """All report rows under ``output_dir`` (``<prefix>*.jsonl``), in file order.

    ``prefix`` selects the report family: ``"batch_report"`` (digitization, the
    default) or ``"publish_report"`` (catalog import).
    """
    rows: list[dict[str, Any]] = []
    for path in sorted(Path(output_dir).glob(f"{prefix}*.jsonl")):
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def latest_per_book(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate to the most recent row per ``(source, book_id)``."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        latest[(row.get("source", "?"), row.get("book_id", "?"))] = row
    return list(latest.values())


def summarize_reports(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline totals over report rows (call :func:`latest_per_book` first)."""
    by_status: dict[str, int] = {}
    for row in rows:
        status = row.get("status", "?")
        by_status[status] = by_status.get(status, 0) + 1

    ok = [r for r in rows if r.get("status") == "ok"]
    pages = sum(r.get("n_pages") or 0 for r in ok)
    pdf_mb = sum(r.get("pdf_mb") or 0.0 for r in ok)
    minutes = sum(r.get("minutes") or 0.0 for r in rows)
    chars = sum(r.get("ocr_chars") or 0 for r in ok)

    return {
        "books": len(rows),
        "by_status": by_status,
        "produced": len(ok),
        "pages": pages,
        "pdf_mb": round(pdf_mb, 1),
        "mb_per_page": round(pdf_mb / pages, 3) if pages else None,
        "ocr_chars": chars,
        "minutes": round(minutes, 1),
        "pages_per_min": round(pages / minutes, 1) if minutes else None,
        "mean_minutes_per_book": round(minutes / len(rows), 2) if rows else None,
    }


def summarize_by_source(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-source headline totals."""
    sources = sorted({r.get("source", "?") for r in rows})
    return {src: summarize_reports([r for r in rows if r.get("source") == src]) for src in sources}


def summarize_publish(output_dir: Path) -> dict[str, Any] | None:
    """Catalog-import status counts from ``publish_report*.jsonl``, or None if none.

    Returns ``{"books": N, "by_status": {published/skipped/error: ...}}`` over the
    latest row per book, so the dashboard can show import progress alongside
    digitization.
    """
    rows = latest_per_book(load_reports(output_dir, prefix="publish_report"))
    if not rows:
        return None
    by_status: dict[str, int] = {}
    for row in rows:
        status = row.get("status", "?")
        by_status[status] = by_status.get(status, 0) + 1
    return {"books": len(rows), "by_status": by_status}
