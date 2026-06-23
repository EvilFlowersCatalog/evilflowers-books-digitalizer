# Operations runbook

## Monitoring a run

- **Prefect UI** (`:4200`): live flow/task graph, per-task logs and timings.
- **Per-book artifact** (`book-<src>-<id>`): status, pages, MB, OCR chars,
  language, catalog match, minutes — or the error.
- **Per-source artifact** (`summary-<src>`): one table row per book + status
  tally in the description.
- **JSONL report**: `output/batch_report_<src>.jsonl`, one row per book (parity
  with the notebook-05 batch report; survives UI retention).

## Resuming

Re-running a source or corpus flow is safe and cheap: `process_book` skips any
book whose final PDF already exists, so completed books return `skipped` quickly.
Just trigger the same deployment again after an interruption.

## Disk

Low-storage mode is on by default: staged frames (symlinks), ScanTailor output
and the pre-OCR PDF are deleted per book; only `<slug>.pdf`, `<slug>.txt` and
`<slug>.cover.jpg` remain. A worker waits (up to `disk_wait_minutes`) for space
when free disk drops below `[orchestration].min_free_gb`, then errors if it never
recovers. Offload `output/` to durable storage periodically (the whole corpus is
~65–70 GB of PDFs).

## Concurrency tuning

`max_parallel_books * ocr_jobs ≤ CPU cores`. Start conservative (e.g. 4 × 2 on an
8-core VM) and watch CPU/disk in the UI. `max_parallel_books` is read at flow
import, so change it in `configs/pipeline.toml` and restart the worker.

## Common issues

| Symptom | Likely cause / fix |
|---|---|
| Source flow Failed, most books errored | Systematic problem — `scantailor-deviant-cli` / `recode_pdf` / tesseract missing in the worker image, or the mount unreadable. Check a per-book artifact's error. |
| All titles are de-slugged directory names | Catalog didn't match — run `validate-catalog`, check `[metadata].columns` and `key_field`. |
| Covers missing for some books | Cover errors are non-fatal (logged). Check the book's task log; usually a font/metadata edge case. |
| `low disk` errors | Free space below `min_free_gb`; offload `output/` or raise disk. |

## Catalog & cover iteration

```bash
python -m evilflowers_books_digitalizer validate-catalog          # match/miss across all sources
python -m evilflowers_books_digitalizer preview-cover svf <book_id> --template banner
```

`preview-cover` needs only the Excel (not the mount), so you can iterate on
palette/template quickly, then re-deploy.
