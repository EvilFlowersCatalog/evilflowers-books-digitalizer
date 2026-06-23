# Architecture

The digitalizer turns raw flatbed TIFF scans into compact, searchable PDF/A
documents plus a stylish cover thumbnail, orchestrated by Prefect for unattended
multi-TB runs on a remote VM.

## Layers

```
┌── orchestration (Prefect) ──────────────────────────────────────────────┐
│  digitize_corpus → digitize_source → digitize_book   (retries, limits,   │
│                                          per-book/per-source artifacts)  │
└──────────────────────────────────┬───────────────────────────────────────┘
                                    │ each book, isolated process
            ┌───────────────────────▼─────────────────────────────────┐
            │ pipeline (PipelineStep / Pipeline / BookContext)         │
            │  download → scantailor → [docres] → language → mrc       │
            │           → metadata → enrich → cover → finalize         │
            └───┬─────────────┬─────────────────┬──────────────────────┘
                │             │                 │
          sources/      metadata/           covers/
       (local mount,   (Excel catalog,    (PIL templates,
        WebDAV)         dir-name keyed)     faculty palettes)
```

## Components

| Module | Responsibility |
|---|---|
| `sources/` | `AbstractBookSource` + `FilesystemBookSource` (local mount, production) and `BookSource` (WebDAV). `build_source` picks the backend from `[source]`. |
| `pipeline/` | The step framework (`base.py`), engine assembly (`factory.py`), and the steps (`steps/`). |
| `metadata/` | `MetadataCatalog.from_excel` — bibliographic records matched to books by directory name. |
| `covers/` | `CoverRenderer` + templates + per-faculty palettes; bundled DejaVu fonts (full Slovak coverage). |
| `orchestration/` | Prefect flows wrapping `batch.process_book`. |
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
process (`batch.process_book`, invoked by `digitize_book` via a 1-worker
`ProcessPoolExecutor`). This also bounds memory growth over a long batch and
makes every book independently retryable and resumable (skip when the final PDF
already exists). Prefect's `ThreadPoolTaskRunner` bounds how many books run at
once; `[orchestration].ocr_jobs` bounds threads *inside* each book — keep
`max_parallel_books * ocr_jobs ≤ CPU cores`.

## Scaling

- **Multi-TB input**: the filesystem source *symlinks* frames off the mount
  (zero-copy); ScanTailor writes cleaned pages to local scratch, which is deleted
  per book (low-storage mode).
- **More throughput**: raise `max_parallel_books` (bigger VM) or add Prefect
  workers pointing at the same work pool — `digitize_book` is self-contained and
  idempotent, so multiple workers can drain the queue.
- **Downstream** (planned): the OCR text sidecars feed embeddings + graph
  classification via additional `PipelineStep`s, no changes to existing stages.
