# EvilFlowers Books Digitalizer

[![Tests](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/actions/workflows/tests.yml/badge.svg)](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/actions/workflows/tests.yml)
[![Docker](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/actions/workflows/docker.yml/badge.svg)](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/actions/workflows/docker.yml)
[![Documentation](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/actions/workflows/docs.yml/badge.svg)](https://evilflowerscatalog.github.io/evilflowers-books-digitalizer/)

📖 **Documentation:** <https://evilflowerscatalog.github.io/evilflowers-books-digitalizer/>

Digitalization pipeline for scanned books: TIFF scans → **two PDFs per book** —
a fast **distribution** copy and a **PDF/A-2b archival** master — with Slovak
OCR, metadata and covers, then imported into the
[EvilFlowers Catalog](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog).
Built entirely on OSS tooling (Tesseract, ScanTailor, archive-pdf-tools, pikepdf,
webdav4).

Planned follow-ups on the same pipeline: embeddings for a vector database and
richer classification for a graph database.

## Architecture

Scans are **two-page spreads on a black scanner bed** (uncompressed RGB TIFF @ 300
DPI, ~24 MB/frame). Frames are read by a pluggable **source** (a local mount in
production, WebDAV in dev — `[source]` in `configs/pipeline.toml`). One OCR pass
feeds a profile-driven renderer that emits **one PDF per output profile**:

```
source (TIFF frames per book)        output/distribution/<source>/<slug>.pdf,.txt,.cover.jpg
        │                                   output/archival/<source>/<slug>.pdf  (PDF/A-2b)
        ▼                                                          ▲
  DownloadBook ─► [AttachMetadata] ─► ScanTailorScans ─► [DocResEnhance]
  (stage to        (Excel catalog)     (split at gutter,  (optional AI
   local cache)                         deskew, margins)   appearance)
        │
        ▼
  [DetectLanguage] ─► OcrPages ─────► RenderPdf ──────────► EnrichPdfMetadata
   (langdetect)       (Tesseract       (recode_pdf MRC,      (XMP into the
                       -> hOCR + txt)   one PDF per profile)  non-PDF/A copies)
        │
        ▼
  [GenerateCover] ─► FinalizePdf ──────► EnsurePdfA ─────► [WriteCatalogManifest]
   (thumbnail)       (merged bookmarks,  (declare archival   (entry.json for the
                      page labels,        as PDF/A-2b,        distribution copy)
                      linearize dist.)    veraPDF-validated)
```

**Output profiles** (`[render]`, presets in `pipeline/profiles.py`):

| profile | encoding | for | notes |
|---|---|---|---|
| `distribution` | **JPEG (DCTDecode)** MRC, bg↓3 fg↓2, linearized | readers, the catalog | opens fast in every viewer; ~7× faster to decode than JPEG2000 |
| `archival` | **JPEG2000** MRC → **PDF/A-2b** | preservation master | compact; opened rarely |

Text stays razor-sharp in both — the JBIG2 1-bit mask is full resolution; only
the colour layers downsample. (The old single output used 26-MP JPEG2000 layers
that rendered blank/slow in Preview and PDF.js — fixed by the JPEG distribution
copy. See `notebooks/03_dual_output_lab.ipynb`.)

**Output layout** (`[render] layout`): `split` (default) separates the trees —
`<output>/distribution/<faculty>/<slug>.pdf` (+ sidecars/cover) and
`<output>/archival/<faculty>/<slug>.pdf` — so access copies can be served and
masters cold-stored independently. `flat` instead writes both PDFs next to the
sidecars as `<slug>.<profile>.pdf`. Unreadable/truncated source frames are
skipped (recorded in the report's `skipped_frames`) rather than failing the book.

Each step implements `PipelineStep.run(ctx: BookContext) -> BookContext`; a
`Pipeline` is an ordered list of steps, so planned stages (classification,
embeddings on the OCR text sidecar) and new outputs (a new `PdfProfile`) slot in
without touching existing code. `pipeline.factory.build_pipeline` wires it from
`configs/pipeline.toml`.

The empirical basis for the settings is in
[docs/digitalization_pipeline_report.md](docs/digitalization_pipeline_report.md)
and reproducible in `notebooks/03_dual_output_lab.ipynb`.

## Setup

The pipeline shells out to system tools (Tesseract OCR, `scantailor-deviant-cli`,
`jbig2enc` for the MRC text mask, **`jpegoptim`** for the JPEG distribution PDF,
and `qpdf` for linearization). Install those for your platform, then the Python
package. Or skip the host install entirely and use
**[Docker](#docker-full-toolchain)**, which bakes in the full toolchain.

### 1. System dependencies

**macOS (Homebrew)**

```bash
brew install tesseract tesseract-lang               # required (OCR)
brew install jbig2enc jpegoptim qpdf                # MRC mask + JPEG output + linearize
brew install verapdf                                # optional: PDF/A-2b validation
```

**Ubuntu / Debian (apt)**

```bash
sudo apt update && sudo apt install -y \
  tesseract-ocr tesseract-ocr-slk tesseract-ocr-ces tesseract-ocr-eng \
  jbig2enc jpegoptim qpdf
```

`scantailor-deviant-cli` has no distro/brew package on either platform. Build it
from source (recipe in the [Dockerfile](Dockerfile)) and drop the binary in
`~/.local/bin`, or use Docker. Then verify Tesseract has the Slovak pack:

```bash
tesseract --list-langs | grep slk
```

### 2. Python package

With [uv](https://docs.astral.sh/uv/) (recommended) or Poetry:

```bash
git clone https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer.git
cd evilflowers-books-digitalizer
uv sync                                             # or: poetry install
cp credentials.example.toml credentials.toml        # WebDAV creds (dev/webdav source only)
```

Run the operator CLI:

```bash
uv run evilflowers-digitalizer --help               # or: poetry run … / python -m evilflowers_books_digitalizer …
```

**Try the CLI without cloning** — `uvx` runs it straight from the public repo (you
still need the system tools above and a `configs/pipeline.toml` in the working dir):

```bash
uvx --from git+https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer \
  evilflowers-digitalizer --help
```

### Docker (full toolchain)

The image bakes in *everything* the `scantailor_mrc` engine needs —
`scantailor-deviant-cli`, `jbig2enc`, Tesseract + language packs, and the optional
DocRes model — so no host install is required:

```bash
docker build -t evilflowers-digitalizer .            # --build-arg DOCRES_WEIGHTS=0 to skip ~900 MB
docker run --rm -it \
  -v /mnt/digital-library/raw-scans:/mnt/digital-library/raw-scans:ro \
  -v $PWD/configs:/app/configs:ro \
  -v $PWD/output:/app/output -v $PWD/.cache:/app/.cache \
  evilflowers-digitalizer \
  python -m evilflowers_books_digitalizer run-book fad '<BOOK_ID>'
```

> **DocRes (optional AI enhancement).** On Apple Silicon the host MPS path is far
> faster than the container's CPU; it lives in `~/.local/share/evilflowers-tools/`
> (DocRes repo + venv + weights), configured under `[docres]` in `pipeline.toml`.

## Production (local mount, plain runner)

For unattended multi-TB runs on a VM, the scans are a **local mount**
(`/mnt/digital-library/raw-scans`), books are enriched from an **Excel catalog**,
and each gets a **cover thumbnail** (real OPAC cover by ISBN, else generated). The
batch is a single long-running process — **no orchestrator, server, or database**.
Configure everything in `configs/pipeline.toml`.

```bash
evilflowers-digitalizer <command>        # or: python -m evilflowers_books_digitalizer <command>
  list svf                   # books on a source
  validate-catalog           # Excel match/miss report across sources
  preview-cover svf <id>     # render one cover to iterate on style
  run-book svf <id>          # one book, end to end
  run-source svf --limit 3   # a faculty (process pool)
  run-corpus                 # the whole corpus
  monitor                    # live TUI dashboard (progress, throughput, ETA)
  stats                      # snapshot table (--json | --export csv|json|html)
  publish-catalog            # import produced books into EvilFlowers Catalog
```

Run it under screen / systemd / Docker — see `deploy/` and:

- [docs/testing.md](docs/testing.md) — **end-to-end testing & go-live runbook** (start here)
- [docs/architecture.md](docs/architecture.md) — components and data flow
- [docs/deployment.md](docs/deployment.md) — production config + go-live
- [deploy/README.md](deploy/README.md) — run (screen/systemd/Docker) + monitoring
- [docs/operations.md](docs/operations.md) — monitoring, resume, disk, tuning
- [docs/metadata_and_covers.md](docs/metadata_and_covers.md) — Excel mapping + cover sourcing
- [docs/catalog_import.md](docs/catalog_import.md) — manifest → Catalog REST import

## Notebooks

Experimentation happens in notebooks first; the module holds the reusable parts.
Every notebook shares the **single cache + output root** via `load_runtime()` —
no notebook hardcodes its own scratch dir. Install the notebook dependencies
(`poetry install --with notebooks`), then `poetry run jupyter lab`.

| Notebook                          | Purpose                                                                  |
|-----------------------------------|--------------------------------------------------------------------------|
| `01_explore_corpus.ipynb`         | Survey sources + the local cache; inspect a raw frame                    |
| `02_clean_pages_lab.ipynb`        | ScanTailor cleaning lab: before/after, knobs                             |
| `03_dual_output_lab.ipynb`        | **The encoder experiment**: JPEG vs JPEG2000, size + decode time, PDF/A, alternatives |
| `04_single_book_e2e.ipynb`        | Full production pipeline on one cached book; inspect both PDFs           |
| `05_batch_pipeline.ipynb`         | Batch over books (resumable, low-storage, monitor)                       |
| `06_metadata_and_covers.ipynb`    | Metadata draft + ISBN enrichment + cover preview                         |
| `07_stats_and_results.ipynb`      | Stats dashboard over `output/`: coverage, sizes, throughput              |

Programmatic use:

```python
from evilflowers_books_digitalizer import BookContext, LocalCache, build_source
from evilflowers_books_digitalizer.pipeline.factory import build_pipeline
from evilflowers_books_digitalizer.runtime import build_catalog, load_runtime

rt = load_runtime()                                  # single cache + output root
cache = LocalCache(rt.cache_dir)
source = build_source(rt.source, "svf")
pipeline = build_pipeline(source, cache, config=rt.config, catalog=build_catalog(rt.metadata))

ctx = pipeline.run(BookContext(
    source="svf", book_id="<book-dir>",
    work_dir=cache.book_dir("svf", "<book-dir>"),
    output_dir=rt.output_dir / "svf",
))
print(ctx.artifacts["pdf_distribution"], ctx.artifacts["pdf_archival"])
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
    ├── factory.py        build_pipeline (imaging → render → enrich → finalize → pdfa tail)
    ├── profiles.py       PdfProfile + DISTRIBUTION / ARCHIVAL presets
    ├── hocr.py           shared hOCR parser (OCR sidecar + finalize bookmarks)
    ├── pdfa.py           lossless PDF/A-3b → 2b re-declaration + veraPDF validation
    └── steps/            download, scantailor, ocr, render, enrich, cover, finalize, pdfa, …
configs/pipeline.toml     production config (source, metadata, cover, orchestration)
configs/catalog.xlsx      bibliographic metadata (you provide)
deploy/                   run the batch: systemd unit, docker-compose, runbook
docs/                     architecture, deployment, operations, metadata & covers
notebooks/                exploration & experimentation
credentials.toml          WebDAV secrets (gitignored; not needed for the mount backend)
.cache/                   staged scans + share stats (gitignored)
output/                   produced PDFs / text / covers (gitignored)
```
