"""Export batch results for handover: per-book CSV, summary JSON, HTML report.

The ``stats`` command renders a terminal snapshot; these writers turn the same
reports into shareable files — a spreadsheet for librarians (CSV), a machine
payload (JSON), and a self-contained styled report with bars (HTML, no external
assets) for sending around. Pure: takes rows/summaries, writes one file.
"""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.reporting import summarize_by_source, summarize_reports

#: Per-book CSV columns, in order (a stable subset of the batch report row).
CSV_FIELDS = (
    "source", "book_id", "status", "title", "catalog_matched", "language",
    "n_frames", "n_pages", "pdf_mb", "ocr_chars", "minutes", "pdf", "cover",
    "manifest", "error",
)

FORMATS = ("csv", "json", "html")


def default_path(output_dir: Path, fmt: str, source: str | None = None) -> Path:
    """Where an export lands when ``--out`` is omitted."""
    stem = f"stats_{source}" if source else "stats"
    return Path(output_dir) / f"{stem}.{fmt}"


def write_csv(rows: list[dict[str, Any]], path: Path) -> Path:
    """One row per book, stable columns — opens cleanly in Excel/LibreOffice."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (r.get("source", ""), r.get("book_id", ""))):
            writer.writerow({k: row.get(k) for k in CSV_FIELDS})
    return path


def write_json(rows: list[dict[str, Any]], path: Path) -> Path:
    """Headline summary (overall + per-source) as JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"overall": summarize_reports(rows), "by_source": summarize_by_source(rows)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _bar(done: int, total: int | None) -> str:
    pct = min(100 * done / total, 100) if total else 0
    label = f"{done}/{total}" if total else str(done)
    return (
        f'<div class="bar"><div class="fill" style="width:{pct:.0f}%"></div>'
        f'<span>{label}</span></div>'
    )


def write_html(
    rows: list[dict[str, Any]],
    path: Path,
    *,
    totals: dict[str, int] | None = None,
    publish: dict[str, Any] | None = None,
    title: str = "EvilFlowers digitizer — results",
) -> Path:
    """A self-contained HTML report (inline CSS, no external assets)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    totals = totals or {}
    overall = summarize_reports(rows)
    by_source = summarize_by_source(rows)

    def cells(name: str, s: dict[str, Any], total: int | None) -> str:
        st = s["by_status"]
        ppm = f"{s['pages_per_min']:.0f}" if s["pages_per_min"] else "—"
        return (
            f"<td class='src'>{html.escape(name)}</td>"
            f"<td>{_bar(s['books'], total)}</td>"
            f"<td class='ok'>{st.get('ok', 0)}</td><td class='skip'>{st.get('skipped', 0)}</td>"
            f"<td class='err'>{st.get('error', 0)}</td><td>{s['pages']:,}</td>"
            f"<td>{s['pdf_mb']:.0f}</td><td>{s['minutes']:.0f}</td><td>{ppm}</td>"
        )

    body = [
        f"<tr>{cells(src, by_source[src], totals.get(src))}</tr>"
        for src in sorted(by_source)
    ]
    grand_total = sum(totals.values()) if totals else None
    head = (
        f"books {overall['books']}" + (f" / {grand_total}" if grand_total else "")
        + f" &middot; output {overall['pdf_mb'] / 1000:.1f} GB"
        + (f" &middot; {overall['mb_per_page']:.2f} MB/page" if overall["mb_per_page"] else "")
        + f" &middot; {overall['pages']:,} pages"
    )
    publish_html = ""
    if publish:
        st = publish["by_status"]
        publish_html = (
            f"<p class='pub'>Catalog import: {publish['books']} books &middot; "
            f"<span class='ok'>{st.get('published', 0)} published</span> &middot; "
            f"<span class='skip'>{st.get('skipped', 0)} skipped</span> &middot; "
            f"<span class='err'>{st.get('error', 0)} error</span></p>"
        )

    path.write_text(_HTML.format(
        title=html.escape(title),
        head=head,
        rows="\n".join(body),
        total_cells=cells("TOTAL", overall, grand_total),
        publish=publish_html,
    ), encoding="utf-8")
    return path


def export_report(
    rows: list[dict[str, Any]],
    fmt: str,
    path: Path,
    *,
    totals: dict[str, int] | None = None,
    publish: dict[str, Any] | None = None,
) -> Path:
    """Dispatch to the writer for ``fmt`` (``csv`` | ``json`` | ``html``)."""
    if fmt == "csv":
        return write_csv(rows, path)
    if fmt == "json":
        return write_json(rows, path)
    if fmt == "html":
        return write_html(rows, path, totals=totals, publish=publish)
    raise ValueError(f"unknown export format {fmt!r}; have {FORMATS}")


_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 60rem; padding: 0 1rem; }}
  h1 {{ font-size: 1.3rem; margin-bottom: .2rem; }}
  .head {{ color: #555; margin-bottom: 1.5rem; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: .45rem .6rem; text-align: right; border-bottom: 1px solid #ddd; }}
  th:first-child, td.src {{ text-align: left; font-weight: 600; }}
  tfoot td {{ font-weight: 700; border-top: 2px solid #888; }}
  .ok {{ color: #1a7f37; }} .skip {{ color: #9a6700; }} .err {{ color: #cf222e; }}
  .bar {{ position: relative; background: #eee; border-radius: 4px; height: 1.1rem; min-width: 8rem; }}
  .fill {{ position: absolute; inset: 0; width: 0; background: #2da44e; border-radius: 4px; }}
  .bar span {{ position: relative; font-size: .8rem; padding: 0 .4rem; line-height: 1.1rem; }}
  .pub {{ margin-top: 1.5rem; padding: .8rem; background: #f6f8fa; border-radius: 6px; }}
</style></head>
<body>
  <h1>{title}</h1>
  <p class="head">{head}</p>
  <table>
    <thead><tr><th>source</th><th>progress</th><th>ok</th><th>skip</th><th>err</th>
      <th>pages</th><th>MB</th><th>min</th><th>pg/min</th></tr></thead>
    <tbody>
{rows}
    </tbody>
    <tfoot><tr>{total_cells}</tr></tfoot>
  </table>
  {publish}
</body></html>
"""
