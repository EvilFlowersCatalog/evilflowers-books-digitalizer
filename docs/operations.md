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
- **Log** — stderr and `output/logs/digitizer.log`. Text by default;
  `EVILFLOWERS_LOG_FORMAT=json` gives one JSON object per line with `source`,
  `book_id`, `step`, `duration_ms`, `status`, `entry_id` attached (format in
  [deployment.md](deployment.md#logs)) — filter by book or step in Loki.
  `EVILFLOWERS_LOG_LEVEL` sets the level.
- **JSONL reports** — `output/batch_report_<src>.jsonl` (digitization) and
  `output/publish_report_<src>.jsonl` (catalog import), one row per book (re-runs
  append; `stats`/`monitor` dedup to the newest per book).

## Resuming

Re-running `run-book`/`run-source`/`run-corpus` is safe and cheap: `process_book`
skips any book whose distribution PDF already exists, so completed books return
`skipped` quickly. Just start the same command again after an interruption or
reboot. Under systemd, `Restart=on-failure` does this automatically; a k8s Job
just re-runs the same `run-book … --publish`.

Before a run, `evilflowers-digitalizer check` (add `--catalog` when publishing)
catches unreadable mounts, read-only volumes, low disk and missing binaries.

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
| `low disk` errors | Free space below `min_free_gb` on `cache_dir`'s filesystem; offload `output/` or raise disk. |
| `check` fails `… writable` in a container | The uid the container runs as cannot write the volume; fix ownership or `--user`. |
| Publish row `conflict` | The catalog already holds a *different* book with that title/ISBN. Fix the title/ISBN or route it via `cieľový_katalog`, then re-run. |
| Publish row `error` with an `entry_id` | The upload failed after the entry was created; just re-run — it resumes into the same entry. |

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
evilflowers-digitalizer check --catalog              # token, ping, target catalogs
evilflowers-digitalizer publish-catalog --dry-run    # validate payloads, no HTTP
evilflowers-digitalizer publish-catalog              # resumable push
```

Re-runs are safe: books already recorded `published`/`skipped` are skipped, an
interrupted book resumes into the entry it already created, and a 409 is only
adopted when it is provably the same book. Progress shows in `stats`/`monitor`.

## Handover exports

```bash
python -m evilflowers_books_digitalizer stats --export csv    # per-book spreadsheet
python -m evilflowers_books_digitalizer stats --export html   # shareable styled report
python -m evilflowers_books_digitalizer stats svf --export csv # one faculty only
```

Files land at `output/stats[_<source>].<ext>` unless `--out` is given.
