# Architecture

The digitalizer turns raw flatbed TIFF scans into compact, searchable PDF/A
documents plus a cover thumbnail. It runs as a plain long-running process (no
orchestrator/server/DB) for unattended multi-TB runs on a remote VM.

## Layers

```
┌── runner (process pool) ────────────────────────────────────────────────┐
│  run_corpus → run_source → process_book   (per-book isolated process,    │
│                                             JSONL report, resume-on-skip) │
└──────────────────────────────────┬───────────────────────────────────────┘
                                    │ each book, isolated process
            ┌───────────────────────▼─────────────────────────────────┐
            │ pipeline (PipelineStep / Pipeline / BookContext)         │
            │  download → scantailor → [docres] → language → mrc       │
            │           → metadata → enrich → cover → finalize         │
            └───┬─────────────┬─────────────────┬──────────────────────┘
                │             │                 │
          sources/      metadata/           covers/
       (local mount,   (Excel catalog,    (OPAC cover by ISBN,
        WebDAV)         dir-name keyed)     else PIL templates)

   monitor (rich TUI) + stats (CLI) read the JSONL report — out of band.
```

## Components

| Module | Responsibility |
|---|---|
| `sources/` | `AbstractBookSource` + `FilesystemBookSource` (local mount, production) and `BookSource` (WebDAV). `build_source` picks the backend from `[source]`. |
| `pipeline/` | The step framework (`base.py`), engine assembly (`factory.py`), and the steps (`steps/`). |
| `batch.py` | `process_book` — one book end to end, isolated, low-storage, resumable. |
| `runner.py` | `run_source`/`run_corpus` — process-pool fan-out over `process_book` with a resumable JSONL report. |
| `metadata/` | `MetadataCatalog.from_excel` (dir-name join) + `isbn_lookup` (STU OPAC / Open Library enrichment). |
| `covers/` | OPAC cover by ISBN (`opac.py`) else `CoverRenderer` templates + per-faculty palettes; bundled DejaVu fonts. |
| `reporting.py` / `monitor.py` | Summaries over the JSONL reports; `monitor` is a live rich TUI. |
| `runtime.py` | Resolves `configs/pipeline.toml` into ready objects (paths, source, catalog). |
| `cli.py` | `python -m evilflowers_books_digitalizer …` operator commands. |

## The engine (chosen from the experiments)

Production uses the **`scantailor_mrc`** engine, V2 profile — see
`docs/digitalization_pipeline_report.md` for the full evaluation:

- ScanTailor `mixed` cleanup at **`output_dpi = 600`** (supersampled binarization
  → smooth glyphs), then Tesseract `slk`/auto hOCR, then `recode_pdf` MRC
  assembly (JBIG2 text mask over JPEG2000 layers) and a fail-safe finalizer.
- ~5× smaller PDFs *and* higher OCR recall than the legacy OpenCV→OCRmyPDF engine.
- **Rejected** after measurement: PERO-OCR, UVDoc, NAF-DPM (negative transfer /
  access-gated). DocRes is optional (color books only). `legacy` is a fallback.

## Why the book is the unit of work

OCRmyPDF / `recode_pdf` are not thread-safe, so each book runs in its own OS
process — `runner.py` fans `batch.process_book` out across a
`ProcessPoolExecutor`. This also bounds memory growth over a long batch and makes
every book independently resumable (skip when the final PDF already exists). The
pool size is `[orchestration].max_parallel_books`; `ocr_jobs` bounds threads
*inside* each book — keep `max_parallel_books * ocr_jobs ≤ CPU cores`.

## Scaling

- **Multi-TB input**: the filesystem source *symlinks* frames off the mount
  (zero-copy); ScanTailor writes cleaned pages to local scratch, which is deleted
  per book (low-storage mode).
- **More throughput**: raise `max_parallel_books` (bigger VM), or run
  `run-source <key>` for different faculties on different machines pointed at the
  same output store — `process_book` is self-contained and idempotent.
- **Downstream** (planned): the OCR text sidecars feed embeddings + graph
  classification via additional `PipelineStep`s, no changes to existing stages.
