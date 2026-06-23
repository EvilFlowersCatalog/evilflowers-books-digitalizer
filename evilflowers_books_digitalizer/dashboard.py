"""Shared ``rich`` dashboard rendering for ``monitor`` (live) and ``stats`` (snapshot).

One renderer, two entry points: the live TUI refreshes it on a timer; the
``stats`` command prints it once. Reads only the JSONL reports under
``output_dir`` (digitization ``batch_report_*`` + catalog ``publish_report_*``),
so it never touches a running batch.
"""

from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.panel import Panel
from rich.table import Table

from evilflowers_books_digitalizer.progress import read_active
from evilflowers_books_digitalizer.reporting import (
    latest_per_book,
    load_reports,
    summarize_by_source,
    summarize_publish,
    summarize_reports,
)
from evilflowers_books_digitalizer.runtime import RuntimeConfig

_COLUMNS = ("source", "progress", "done", "ok", "skip", "err", "pages", "MB", "min", "pg/min")


def book_totals(rt: RuntimeConfig) -> dict[str, int]:
    """Total book count per source (for %/ETA); empty if the mount can't be listed."""
    from evilflowers_books_digitalizer.sources import build_source

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


def _count(value: int, style: str) -> str:
    """A status count, dimmed when zero so the eye lands on what's non-empty."""
    return f"[{style}]{value}[/]" if value else "[dim]0[/dim]"


def _source_row(key: str, summary: dict[str, Any] | None, total: int | None) -> list[str]:
    if summary is None:
        return [key, _bar(0, total), "0", _count(0, "green"), _count(0, "yellow"),
                _count(0, "red"), "-", "-", "-", "-"]
    st = summary["by_status"]
    return [
        key, _bar(summary["books"], total), str(summary["books"]),
        _count(st.get("ok", 0), "green"), _count(st.get("skipped", 0), "yellow"),
        _count(st.get("error", 0), "red"),
        f"{summary['pages']:,}", f"{summary['pdf_mb']:.0f}", f"{summary['minutes']:.0f}",
        f"{summary['pages_per_min']:.0f}" if summary["pages_per_min"] else "-",
    ]


def _header(overall: dict[str, Any], grand_total: int, active: int = 0) -> str:
    done = overall["books"]
    parts = [f"books {done}" + (f"/{grand_total}" if grand_total else "")]
    if active:
        parts.append(f"[cyan]{active} working[/cyan]")
    if grand_total and overall["mean_minutes_per_book"] and done < grand_total:
        eta_min = (grand_total - done) * overall["mean_minutes_per_book"]
        parts.append(f"~ETA {eta_min / 60:.1f} h" if eta_min >= 90 else f"~ETA {eta_min:.0f} min")
    parts.append(f"output {overall['pdf_mb'] / 1000:.1f} GB")
    if overall["mb_per_page"]:
        parts.append(f"{overall['mb_per_page']:.2f} MB/page")
    return "   ·   ".join(parts)


def _fmt_elapsed(seconds: float) -> str:
    secs = int(seconds)
    if secs < 60:
        return f"{secs}s"
    minutes, secs = divmod(secs, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _active_panel(active: list[dict[str, Any]]) -> Panel | None:
    """One row per in-flight worker: book, step progress bar, current step, elapsed."""
    if not active:
        return None
    table = Table.grid(expand=True, padding=(0, 1))
    for col, justify in (("book", "left"), ("progress", "left"),
                         ("step", "left"), ("elapsed", "right")):
        table.add_column(col, justify=justify)  # type: ignore[arg-type]
    for w in active:
        table.add_row(
            w.get("slug", "?"),
            _bar(w.get("step_index", 0), w.get("n_steps") or None, width=14),
            f"[cyan]{w.get('step', '?')}[/cyan]",
            _fmt_elapsed(w.get("elapsed", 0.0)),
        )
    return Panel(table, title=f"working now ({len(active)})", border_style="cyan", expand=True)


def _publish_line(publish: dict[str, Any]) -> str:
    st = publish["by_status"]
    return (
        f"catalog import: {publish['books']} books   ·   "
        f"{_count(st.get('published', 0), 'green')} published   "
        f"{_count(st.get('skipped', 0), 'yellow')} skipped   "
        f"{_count(st.get('error', 0), 'red')} error"
    )


def build(rt: RuntimeConfig, totals: dict[str, int], *, sources: list[str] | None = None) -> Group:
    """Render the dashboard for the given runtime (optionally scoped to ``sources``)."""
    keys = sources or rt.source_keys
    rows = [r for r in latest_per_book(load_reports(rt.output_dir))
            if sources is None or r.get("source") in keys]
    by_source = summarize_by_source(rows)
    overall = summarize_reports(rows)
    grand_total = sum(totals.get(k, 0) for k in keys)

    table = Table(expand=True)
    for col in _COLUMNS:
        table.add_column(col, justify="left" if col in ("source", "progress") else "right")
    for key in keys:
        table.add_row(*_source_row(key, by_source.get(key), totals.get(key)))
    table.add_section()
    table.add_row(*_source_row("[b]TOTAL[/b]", overall if overall["books"] else None,
                               grand_total or None))

    active = read_active(rt.output_dir)
    if sources is not None:
        active = [w for w in active if w.get("source") in keys]

    renderables: list[Any] = [
        Panel(_header(overall, grand_total, active=len(active)),
              title="EvilFlowers digitizer", expand=True),
        table,
    ]
    worker_panel = _active_panel(active)
    if worker_panel is not None:
        renderables.append(worker_panel)
    publish = summarize_publish(rt.output_dir)
    if publish:
        renderables.append(Panel(_publish_line(publish), expand=True, border_style="dim"))
    return Group(*renderables)
