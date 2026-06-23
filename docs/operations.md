# Operations runbook

## Monitoring a run

- **`monitor` TUI** — `python -m evilflowers_books_digitalizer monitor`: live
  per-faculty progress bars, ok/skip/err, pages, output MB, pages/min, ETA, plus a
  **"working now" panel** showing each parallel worker in flight (book, current
  step, a step progress bar, elapsed). Reads the report files + per-book
  heartbeats under `output/.progress/`, so run it from anywhere that sees
  `output_dir` (host, `docker exec`, synced volume) — it doesn't touch the running
  batch. Workers refresh their heartbeat at each step boundary and remove it when
  done; a leftover from a crashed worker is ignored after `progress.DEFAULT_TTL`
  and wiped at the next `run-source`.
- **`stats`** — one-shot snapshot. Prints the same rich table as `monitor` (add a
  `source` to scope it); `--json` for cron/Slack; `--export csv|json|html [--out PATH]`
  to write a handover file (per-book CSV, summary JSON, or a self-contained HTML
  report with bars). Shows the catalog-import tally too when publish reports exist.
- **Log** — console and `output/logs/digitizer.log` (timestamped), one line per
  book completion.
- **JSONL reports** — `output/batch_report_<src>.jsonl` (digitization) and
  `output/publish_report_<src>.jsonl` (catalog import), one row per book (re-runs
  append; `stats`/`monitor` dedup to the newest per book).

## Resuming

Re-running `run-source`/`run-corpus` is safe and cheap: `process_book` skips any
book whose final PDF already exists, so completed books return `skipped` quickly.
Just start the same command again after an interruption or reboot. Under systemd,
`Restart=on-failure` does this automatically.

## Disk

Low-storage mode is on by default: staged frames (symlinks), ScanTailor output
and the pre-OCR PDF are deleted per book; only `<slug>.pdf`, `<slug>.txt` and
`<slug>.cover.jpg` remain. A worker waits (up to `disk_wait_minutes`) for space
when free disk drops below `[orchestration].min_free_gb`, then records an error
for that book if it never recovers. Offload `output/` to durable storage
periodically (the whole corpus is ~65–70 GB of PDFs).

## Concurrency tuning

`max_parallel_books * ocr_jobs ≤ CPU cores`. Start conservative (e.g. 4 × 2 on an
8-core VM) and watch CPU/disk via `monitor` + `top`. Change it in
`configs/pipeline.toml` and restart the run (it resumes).

## Common issues

| Symptom | Likely cause / fix |
|---|---|
| Most books error | Systematic problem — `scantailor-deviant-cli` / `recode_pdf` / `tesseract` missing, or the mount unreadable. Check `output/logs/digitizer.log` or a report row's `error`. |
| All titles are de-slugged directory names | Catalog didn't match — run `validate-catalog`, check `[metadata].columns` and `key_field`. |
| Covers all generated (no real ones) | OPAC/obalkyknih unreachable from the VM, or `[cover].source` set to `generated`. Cover errors are non-fatal. |
| `low disk` errors | Free space below `min_free_gb`; offload `output/` or raise disk. |

## Catalog & cover iteration

```bash
python -m evilflowers_books_digitalizer validate-catalog          # match/miss across all sources
python -m evilflowers_books_digitalizer preview-cover svf <book_id> --template banner
```

`preview-cover` needs only the Excel (not the mount), so you can iterate on
palette/template quickly, then re-deploy.

## Catalog import

Once books are produced, publish them into the EvilFlowers Catalog (see
[catalog_import.md](catalog_import.md) for the full flow and `[catalog]` config):

```bash
export EVILFLOWERS_API_KEY="…"
python -m evilflowers_books_digitalizer publish-catalog --dry-run   # preview, no push
python -m evilflowers_books_digitalizer publish-catalog             # idempotent push
```

Re-runs are safe: books already recorded `published`/`skipped` are skipped, and the
catalog rejects duplicates (409 → `skipped`). Progress shows in `stats`/`monitor`.

## Handover exports

```bash
python -m evilflowers_books_digitalizer stats --export csv    # per-book spreadsheet
python -m evilflowers_books_digitalizer stats --export html   # shareable styled report
python -m evilflowers_books_digitalizer stats svf --export csv # one faculty only
```

Files land at `output/stats[_<source>].<ext>` unless `--out` is given.
