# Testing & go-live guide

End-to-end path: prove the pipeline locally (single book → a small batch with the
live dashboard), then deploy to the VM and start the corpus. Run the steps in
order; each one gates the next.

- **Local** uses `configs/pipeline.local.toml` (WebDAV source, `output_local/`,
  small concurrency).
- **Production** uses `configs/pipeline.toml` (the `/mnt` filesystem source).

---

## 0 · Prerequisites (local machine)

```bash
# system tools the engine shells out to (macOS)
brew install tesseract tesseract-lang ghostscript jbig2enc
#   scantailor-deviant-cli: build from source or use the Docker image (see README)
poetry install                                   # installs pandas, openpyxl, rich, …
cp credentials.example.toml credentials.toml     # fill in WebDAV creds (for the WebDAV path)
```

Check the toolchain is visible:

```bash
for t in scantailor-deviant-cli tesseract recode_pdf; do command -v $t || echo "MISSING $t"; done
tesseract --list-langs | grep -E 'slk|ces|eng'   # language packs present
```

WebDAV needs the **VPN** (to reach `datasilo2.schk.sk`). If you'd rather not use
WebDAV, see *Option B* in step 3 to stage a few books to disk.

---

## 1 · Fast checks (no network, seconds)

```bash
poetry run pytest -q                              # unit + smoke tests
poetry run python -c "from evilflowers_books_digitalizer.pipeline.factory import build_pipeline; \
  from evilflowers_books_digitalizer.runtime import load_runtime; \
  print('config + imports OK')"
```

Green here means the code, config and step wiring are sound.

---

## 2 · Prepare the metadata catalogue (recommended)

Run **`notebooks/10_metadata_catalog_draft.ipynb`** (VPN on) to produce
`configs/catalog_template.xlsx` — one row per book, ISBN-enriched from the STU
catalogue. Then check it lines up with the real directories:

```bash
poetry run python -m evilflowers_books_digitalizer validate-catalog \
  --config configs/pipeline.local.toml
```

For testing you can skip this — unmatched books just get a de-slugged stub title.
For production, save the librarian-completed file as `configs/catalog.xlsx`.

Preview a cover without running the pipeline:

```bash
poetry run python -m evilflowers_books_digitalizer preview-cover svf <BOOK_ID> --out /tmp/cover.jpg
```

---

## 3 · One book, end to end — the fastest real test

Pick a small book and run it directly. This exercises the whole engine
(download → ScanTailor → OCR → MRC → metadata → cover → finalize).

```bash
poetry run python -m evilflowers_books_digitalizer list svf --limit 10   # pick a small one
poetry run python -m evilflowers_books_digitalizer run-book svf <BOOK_ID> \
  --config configs/pipeline.local.toml
```

Check the outputs:

```bash
ls -la output_local/svf/svf_<BOOK_ID>.*        # .pdf  .txt  .cover.jpg
```

Open the PDF (searchable text layer, real margins, no bleed-through), the cover
(real OPAC cover or generated), and confirm the cache was cleaned
(`.cache/scans/svf/<BOOK_ID>` gone).

**Option B — local filesystem instead of WebDAV.** Stage a few books to disk and
flip the backend, to rehearse the production read path:

```bash
poetry run python - <<'PY'
from pathlib import Path
from evilflowers_books_digitalizer.config import load_settings
from evilflowers_books_digitalizer.sources.webdav import BookSource
src = BookSource(load_settings().sources["svf"])
base = Path(".cache/raw-scans/svf/SVF-skeny")
for bid in src.list_books()[:2]:
    src.stage_book(bid, base / bid / "stream_pages_tif", progress=True)
print("staged to", base)
PY
```

Then in `configs/pipeline.local.toml` set `[source] backend = "filesystem"`,
`root = ".cache/raw-scans"`, and `[source.paths] svf = "svf/SVF-skeny"`, and run
`run-book` / `run-source` exactly as above — now it reads from disk like the VM.

---

## 4 · Local batch run + live dashboard

Run a couple of books through the process-pool runner (each book in its own
process), then watch the TUI. No server, no database.

```bash
# terminal 1 — run a few books of one faculty
poetry run python -m evilflowers_books_digitalizer run-source svf --limit 3 \
  --config configs/pipeline.local.toml
```

This writes `output_local/batch_report_svf.jsonl`, logs to console **and**
`output_local/logs/digitizer.log`, and prints the status tally. In a second
terminal, watch it live:

```bash
# terminal 2 — live TUI dashboard (refreshes every 5s; Ctrl-C to exit)
poetry run python -m evilflowers_books_digitalizer monitor \
  --config configs/pipeline.local.toml
```

The dashboard shows per-faculty progress bars, ok/skip/err counts, pages, output
MB, throughput (pages/min) and a rough ETA. `monitor --once` renders a single
frame (good for a quick check or piping).

**Resume test:** interrupt the run (Ctrl-C), then run the same command again —
books whose PDF already exists return `skipped`, so it resumes cheaply.

---

## 5 · See stats & results

```bash
poetry run python -m evilflowers_books_digitalizer stats --config configs/pipeline.local.toml
```

Or open **`notebooks/12_stats_and_results.ipynb`** for the visual dashboard:
corpus inventory, catalogue/enrichment coverage, cover sourcing, batch results
(size distribution, throughput), and a grid of produced covers + a rendered page.

When you're satisfied with quality and numbers locally, go to production.

---

## 6 · Deploy to the VM and start processing

The batch is just a long-running process (`run-corpus`) — run it under
**screen/tmux**, **systemd**, or **Docker**. Full run/monitor mechanics are in
[deploy/README.md](../deploy/README.md). Go-live sequence:

1. **VM prerequisites** — scans mounted at `/mnt/digital-library/raw-scans`; the
   toolchain installed (or use the Docker image); `configs/catalog.xlsx` in
   place; `configs/pipeline.toml` set for production (filesystem source,
   `output_dir` on durable storage, `cache_dir` on fast scratch); and
   `[orchestration] max_parallel_books × ocr_jobs ≤ CPU cores`.

2. **Validate the catalogue:**
   ```bash
   python -m evilflowers_books_digitalizer validate-catalog
   ```

3. **Pilot one faculty** before the whole corpus, and eyeball it:
   ```bash
   python -m evilflowers_books_digitalizer run-source svf --limit 20
   python -m evilflowers_books_digitalizer stats
   ```

4. **Run the corpus** under your chosen supervisor (screen / systemd / Docker —
   see [deploy/README.md](../deploy/README.md)):
   ```bash
   screen -S digitize
   python -m evilflowers_books_digitalizer run-corpus
   #   detach: Ctrl-A D     reattach: screen -r digitize
   ```

5. **While it runs** — `monitor` for the live dashboard, `stats` for one-shot
   numbers, `tail -f output/logs/digitizer.log` for detail. Re-running is safe
   (skips finished books). The run pauses if free disk drops below
   `min_free_gb`; offload `output/` periodically. See
   [operations.md](operations.md) for the troubleshooting table.

---

## Quick reference

| Goal | Command |
|---|---|
| Unit tests | `poetry run pytest -q` |
| List books | `… list svf --limit 10` |
| One book | `… run-book svf <id> --config configs/pipeline.local.toml` |
| Catalogue match report | `… validate-catalog --config configs/pipeline.local.toml` |
| Preview a cover | `… preview-cover svf <id> --out /tmp/c.jpg` |
| Run a faculty | `… run-source svf --limit 2 --config configs/pipeline.local.toml` |
| Run everything | `… run-corpus --config configs/pipeline.toml` |
| Live dashboard | `… monitor --config <config>` |
| One-shot stats | `… stats --config <config>` |

(`…` = `poetry run python -m evilflowers_books_digitalizer`)
