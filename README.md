# EvilFlowers Books Digitalizer

Digitalization pipeline for scanned books: TIFF scans on WebDAV shares →
searchable PDF/A documents (Slovak OCR), enriched with metadata and covers, then
imported into the [EvilFlowers Catalog](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog).
Built entirely on OSS tooling (Tesseract, OCRmyPDF, img2pdf, pikepdf, webdav4).

Planned follow-ups on the same pipeline: embeddings for a vector database and
richer classification for a graph database.

## Architecture

The scans are **two-page spreads on a black scanner bed** (uncompressed RGB
TIFF @ 300 DPI, ~24 MB/frame). Frames are read by a pluggable **source** (a
local mount in production, WebDAV in dev — `[source]` in `configs/pipeline.toml`).
Two engines exist, selected by `[pipeline] engine`:

**`scantailor_mrc`** (production default since 2026-06-07, prototyped in
notebook 06 — uniform page sizes with real margins, no bleed-through, ~5×
smaller PDFs):

```
source (TIFF frames per book)                          output/<source>/<slug>.{pdf,txt,cover.jpg,entry.json}
        │                                                                    ▲
        ▼                                                                    │
  DownloadBook ─► ScanTailorScans ─► [DocResEnhance] ─► DetectLanguage ─► MrcPdf
  (stage to       (split at gutter,   (optional AI        (quick OCR +     (Tesseract -> hOCR;
   local cache)    scantailor-cli:     appearance pass)    langdetect)      recode_pdf MRC PDF
                   deskew, dewarp,                                          + text sidecar)
                   margins, uniform)                                              │
                                                                                  ▼
                 AttachMetadata ─► EnrichPdfMetadata ─► GenerateCover ─► FinalizePdf ─► WriteCatalogManifest
                 (Excel catalog,    (XMP: title,         (stylish cover    (bookmarks,    (catalog entry
                  dir-name keyed)    authors, year)       thumbnail)        page labels)   sidecar .entry.json)
```

Produced books are then imported into the EvilFlowers Catalog with
`publish-catalog` (manifest → REST API, idempotent) — see
[docs/catalog_import.md](docs/catalog_import.md).

**`legacy`** (OpenCV preprocess → img2pdf → OCRmyPDF):

```
  DownloadBook ─► PreprocessScans ─► DetectLanguage ─► AssemblePdf ─► OcrPdf ─► EnrichPdfMetadata
  (local cache)   (OpenCV: split      (quick OCR +      (img2pdf,     (OCRmyPDF, detected
                   spreads, crop bed,  langdetect ->     lossless)     language e.g. slk+eng,
                   trim binding        e.g. "eng+slk")                 PDF/A-2 + text sidecar,
                   shadows, deskew,                                    pngquant/jbig2 optimize)
                   whiten paper)
```

Settings live in `configs/pipeline.toml` (consumed by
`pipeline.factory.build_pipeline`).

Each step implements `PipelineStep.run(ctx: BookContext) -> BookContext`;
a `Pipeline` is just an ordered list of steps, so the planned stages
(classification, embeddings on the OCR text sidecar) slot in without touching
existing ones.

## Setup

```bash
# System dependencies (macOS)
brew install tesseract tesseract-lang ghostscript   # required
brew install jbig2enc                               # required (scantailor_mrc): MRC text mask
brew install unpaper pngquant                       # legacy engine: cleaning + PNG optimization

# Python
poetry install

# Credentials (gitignored)
cp credentials.example.toml credentials.toml        # then fill in WebDAV credentials
```

Tesseract must have the Slovak language pack (`tesseract --list-langs | grep slk`).

The `scantailor_mrc` engine additionally needs `scantailor-deviant-cli` — there is no
brew formula. Either build it from source (qt@5 + cmake; bump `CMAKE_CXX_STANDARD`
to 17 in its `CMakeLists.txt`, build the `scantailor-deviant-cli` target, drop the
binary into `~/.local/bin`) or use the Docker image, which bakes in *all* tooling
including the optional DocRes model:

```bash
docker build -t evilflowers-digitalizer .            # --build-arg DOCRES_WEIGHTS=0 to skip ~900 MB
docker run --rm -it \
  -v $PWD/credentials.toml:/app/credentials.toml:ro \
  -v $PWD/.cache:/app/.cache \
  -v $PWD/output:/app/output \
  evilflowers-digitalizer python -c \
  "from evilflowers_books_digitalizer.batch import process_book; print(process_book('fad', '<BOOK_ID>'))"
```

DocRes on the host (Apple Silicon MPS — much faster than the container's CPU) lives in
`~/.local/share/evilflowers-tools/` (DocRes repo + its venv + weights); paths are set
in `[docres]` in `pipeline.toml`.

## Production (local mount, plain runner)

For unattended multi-TB runs on a VM, the scans are a **local mount**
(`/mnt/digital-library/raw-scans`), books are enriched from an **Excel catalog**,
and each gets a **cover thumbnail** (real OPAC cover by ISBN, else generated).
The batch is a single long-running process — **no orchestrator, server, or
database**. Configure everything in `configs/pipeline.toml` (`[paths]`,
`[source]`, `[metadata]`, `[cover]`, `[orchestration]`).

```bash
# operator CLI (python -m evilflowers_books_digitalizer <command>)
… list svf                       # books on a source
… validate-catalog               # Excel match/miss report across sources
… preview-cover svf <book_id>    # render one cover to iterate on style
… run-book svf <book_id>         # one book, end to end
… run-source svf --limit 3       # a faculty (process pool)
… run-corpus                     # the whole corpus
… monitor                        # live TUI dashboard (progress, throughput, ETA)
… stats                          # snapshot table (--json | --export csv|json|html)
… publish-catalog                # import produced books into EvilFlowers Catalog
```

Run it under screen / systemd / Docker — see `deploy/` and:

- [docs/testing.md](docs/testing.md) — **end-to-end testing & go-live runbook** (start here)
- [docs/architecture.md](docs/architecture.md) — components and data flow
- [docs/deployment.md](docs/deployment.md) — production config + go-live
- [deploy/README.md](deploy/README.md) — run (screen/systemd/Docker) + monitoring
- [docs/operations.md](docs/operations.md) — monitoring, resume, disk, tuning
- [docs/metadata_and_covers.md](docs/metadata_and_covers.md) — Excel mapping + cover sourcing

The empirical basis for the engine choice is in
[docs/digitalization_pipeline_report.md](docs/digitalization_pipeline_report.md).

## Usage

Experimentation happens in notebooks first; the module holds the reusable parts.

| Notebook                                   | Purpose                                                                                                                                                                                      |
|--------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `notebooks/01_webdav_exploration.ipynb`    | Inventory the shares: books, pages, sizes — interactive Plotly charts (cached to `.cache/stats/`)                                                                                            |
| `notebooks/02_single_book_pipeline.ipynb`  | Download one book and run the standard pipeline end to end                                                                                                                                   |
| `notebooks/03_transformation_lab.ipynb`    | Tune the TIFF → PDF/A transformation: spread splitting, unpaper, compression/quality matrix, OCR confidence                                                                                  |
| `notebooks/04_produce_samples.ipynb`       | Run the production pipeline on one small book per faculty                                                                                                                                    |
| `notebooks/05_batch_pipeline.ipynb`        | Batch run: 4 books in parallel (process pool), low-storage mode (cache deleted per book), resumable via `output/batch_report.jsonl`                                                          |
| `notebooks/06_scantailor_mrc_lab.ipynb`    | ScanTailor + MRC engine lab: run one cached book through the new engine, eyeball pages, compare against legacy output                                                                        |
| `notebooks/07_fonts_tables_diagrams.ipynb` | Variant comparison (mixed@300/600, grayscale, DocRes): font quality at 3× zoom, table/diagram survival checks, corpus inventory of figure-heavy books                                        |
| `notebooks/08_perfect_quality_lab.ipynb`   | Max-quality experiments: Sauvola/Wolf supersampled masks, facsimile-grade MRC profile (V5), potrace vector-glyph demo, prioritized roadmap (PERO-OCR, UVDoc, NAF-DPM, neural SR, smoothscan) |
| `notebooks/09_finalizer_pero_pilot.ipynb`  | Finalizer + PERO/UVDoc/NAF-DPM pilots and the V2–V5 decision (see the report)                                                                                                                |
| `notebooks/10_metadata_catalog_draft.ipynb`| **Draft the librarian metadata Excel** — one pre-filled row per book (directory_id, faculty, derived ISBN, guessed title), styled with an instructions sheet                                  |
| `notebooks/11_local_e2e_experiment.ipynb`  | **Local end-to-end test** on a few small books via `configs/pipeline.local.toml` (WebDAV source); inspect report, covers, rendered pages                                                       |
| `notebooks/12_stats_and_results.ipynb`     | **Stats & results dashboard** — corpus inventory, catalogue/enrichment coverage, cover sourcing, batch results (sizes, throughput), sample outputs                                            |

> Heads-up: Ghostscript ≥ 10.6 has a known JPEG-encoding bug affecting PDF/A
> conversion; OCRmyPDF mitigates it, but visually check outputs (notebook 03,
> "pixel peeping") after Ghostscript upgrades.

```bash
poetry run jupyter lab
```

Programmatic use:

```python
from evilflowers_books_digitalizer import BookContext, BookSource, LocalCache, Pipeline, load_settings
from evilflowers_books_digitalizer.pipeline.steps import AssemblePdf, DownloadBook, OcrPdf

settings = load_settings()
cache = LocalCache(settings.cache_dir)
source = BookSource(settings.sources["svf"])

pipeline = Pipeline([DownloadBook(source, cache), AssemblePdf(), OcrPdf(language="slk")])
ctx = pipeline.run(BookContext(
    source="svf", book_id="<book-dir>",
    work_dir=cache.book_dir("svf", "<book-dir>"),
    output_dir=settings.output_dir / "svf",
))
print(ctx.artifacts["pdf"])
```

## Layout

```
evilflowers_books_digitalizer/
├── config.py             settings + credentials.toml loading
├── runtime.py            resolves pipeline.toml -> paths, source, catalog
├── models.py             BookRef, PageScan, SourceStats
├── cache.py              LocalCache — scans & cached stats layout
├── batch.py              process_book — one book, low-storage, isolated
├── cli.py                operator CLI (python -m evilflowers_books_digitalizer)
├── sources/              AbstractBookSource + filesystem (mount) / webdav backends
├── metadata/             Excel catalog matched to books by directory name
├── covers/               PIL cover renderer, templates, palettes, bundled fonts
├── catalog/              import layer: book -> EvilFlowers Catalog entry (manifest + REST)
├── runner.py             run_source / run_corpus — process-pool batch (no orchestrator)
├── reporting.py          summarize the JSONL reports (powers stats)
├── progress.py           per-book heartbeats (live parallel-worker view)
├── dashboard.py          shared rich table for monitor + stats
├── exports.py            stats exports (per-book CSV, summary JSON, HTML report)
├── monitor.py            live rich TUI dashboard
└── pipeline/
    ├── base.py           BookContext, PipelineStep, Pipeline
    ├── factory.py        build_pipeline (engine + metadata/cover tail)
    └── steps/            download, scantailor, mrc, metadata, enrich, cover, finalize, …
configs/pipeline.toml     production config (source, metadata, cover, orchestration)
configs/catalog.xlsx      bibliographic metadata (you provide)
deploy/                   run the batch: systemd unit, docker-compose, runbook
docs/                     architecture, deployment, operations, metadata & covers
notebooks/                exploration & experimentation
credentials.toml          WebDAV secrets (gitignored; not needed for the mount backend)
.cache/                   staged scans + share stats (gitignored)
output/                   produced PDFs / text / covers (gitignored)
```
