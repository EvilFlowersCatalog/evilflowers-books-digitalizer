"""Live TUI dashboard over the batch JSONL reports (rich).

Decoupled from the runner: the runner writes ``batch_report_*.jsonl``; this reads
them and refreshes the shared :mod:`dashboard` view. So you can watch progress
from anywhere that can see ``output_dir`` — the same machine, ``docker exec``, or
over a synced volume — without touching the running batch.

    python -m evilflowers_books_digitalizer monitor --config configs/pipeline.toml
"""

from __future__ import annotations

import time

from rich.console import Console

from evilflowers_books_digitalizer import dashboard
from evilflowers_books_digitalizer.runtime import load_runtime


def monitor(config_path: str | None = None, interval: float = 5.0, once: bool = False) -> None:
    """Render the dashboard once, or live-refresh every ``interval`` seconds."""
    from rich.live import Live

    rt = load_runtime(config_path)
    console = Console()
    totals = dashboard.book_totals(rt)
    if once:
        console.print(dashboard.build(rt, totals))
        return
    with Live(dashboard.build(rt, totals), console=console, refresh_per_second=4) as live:
        try:
            while True:
                time.sleep(interval)
                live.update(dashboard.build(rt, totals))
        except KeyboardInterrupt:
            pass
