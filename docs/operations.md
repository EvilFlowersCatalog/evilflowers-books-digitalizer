# Operations runbook

## Monitoring a run

- **`monitor` TUI** — `python -m evilflowers_books_digitalizer monitor`: live
  per-faculty progress bars, ok/skip/err, pages, output MB, pages/min, ETA.
  Reads the report files, so run it from anywhere that sees `output_dir` (host,
  `docker exec`, synced volume) — it doesn't touch the running batch.
- **`stats`** — one-shot JSON summary (good for cron / a Slack post).
- **Log** — console and `output/logs/digitizer.log` (timestamped), one line per
  book completion.
- **JSONL report** — `output/batch_report_<src>.jsonl`, one row per book
  (re-runs append; `stats`/`monitor` dedup to the newest per book).

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
