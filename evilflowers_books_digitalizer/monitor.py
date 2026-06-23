"""Simple live TUI dashboard over the batch JSONL reports (rich).

Decoupled from the runner: the runner writes ``batch_report_*.jsonl``; this reads
them and refreshes a table. So you can watch progress from anywhere that can see
``output_dir`` — the same machine, ``docker exec``, or over a synced volume —
without touching the running batch.

    python -m evilflowers_books_digitalizer monitor --config configs/pipeline.toml
"""

from __future__ import annotations

import time
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from evilflowers_books_digitalizer.reporting import (
    latest_per_book,
    load_reports,
    summarize_by_source,
    summarize_reports,
)
from evilflowers_books_digitalizer.runtime import RuntimeConfig, load_runtime
from evilflowers_books_digitalizer.sources import build_source


def _book_totals(rt: RuntimeConfig) -> dict[str, int]:
    """Total book count per source (for %/ETA); empty if it can't list."""
    totals: dict[str, int] = {}
    for key in rt.source_keys:
        try:
            totals[key] = len(build_source(rt.source, key).list_books())
        except Exception:  # noqa: BLE001 — no mount/VPN: just show done counts
            pass
    return totals


def _bar(done: int, total: int | None, width: int = 18) -> str:
    if not total:
        return "—"
    frac = min(done / total, 1.0)
    filled = round(frac * width)
    return f"[{'█' * filled}{'░' * (width - filled)}] {100 * frac:3.0f}%"


def render(rt: RuntimeConfig, totals: dict[str, int]) -> Group:
    rows = latest_per_book(load_reports(rt.output_dir))
    by_source = summarize_by_source(rows)
    overall = summarize_reports(rows)

    table = Table(expand=True)
    for col in ("source", "progress", "done", "ok", "skip", "err", "pages", "MB", "min", "pg/min"):
        table.add_column(col, justify="right" if col not in ("source", "progress") else "left")

    grand_total = sum(totals.values()) if totals else 0
    for key in rt.source_keys:
        s = by_source.get(key)
        total = totals.get(key)
        if s is None:
            table.add_row(key, _bar(0, total), "0", "0", "0", "0", "-", "-", "-", "-")
            continue
        st = s["by_status"]
        table.add_row(
            key, _bar(s["books"], total), str(s["books"]),
            str(st.get("ok", 0)), str(st.get("skipped", 0)), str(st.get("error", 0)),
            f"{s['pages']:,}", f"{s['pdf_mb']:.0f}", f"{s['minutes']:.0f}",
            f"{s['pages_per_min']:.0f}" if s["pages_per_min"] else "-",
        )
    table.add_section()
    ost = overall["by_status"]
    table.add_row(
        "[b]TOTAL[/b]", _bar(overall["books"], grand_total or None), f"[b]{overall['books']}[/b]",
        str(ost.get("ok", 0)), str(ost.get("skipped", 0)), str(ost.get("error", 0)),
        f"{overall['pages']:,}", f"{overall['pdf_mb']:.0f}", f"{overall['minutes']:.0f}",
        f"{overall['pages_per_min']:.0f}" if overall["pages_per_min"] else "-",
    )

    header = _header(overall, grand_total)
    return Group(Panel(header, title="EvilFlowers digitizer", expand=True), table)


def _header(overall: dict[str, Any], grand_total: int) -> str:
    done = overall["books"]
    parts = [f"books {done}" + (f"/{grand_total}" if grand_total else "")]
    if grand_total and overall["mean_minutes_per_book"] and done < grand_total:
        # rough ETA: remaining * mean wall-minutes per book (already parallel-amortized)
        eta_min = (grand_total - done) * overall["mean_minutes_per_book"]
        parts.append(f"~ETA {eta_min / 60:.1f} h" if eta_min >= 90 else f"~ETA {eta_min:.0f} min")
    parts.append(f"output {overall['pdf_mb'] / 1000:.1f} GB")
    if overall["mb_per_page"]:
        parts.append(f"{overall['mb_per_page']:.2f} MB/page")
    return "   ·   ".join(parts)


def monitor(config_path: str | None = None, interval: float = 5.0, once: bool = False) -> None:
    """Render the dashboard once, or live-refresh every ``interval`` seconds."""
    rt = load_runtime(config_path)
    console = Console()
    totals = _book_totals(rt)
    if once:
        console.print(render(rt, totals))
        return
    with Live(render(rt, totals), console=console, refresh_per_second=4, screen=False) as live:
        try:
            while True:
                time.sleep(interval)
                live.update(render(rt, totals))
        except KeyboardInterrupt:
            pass
