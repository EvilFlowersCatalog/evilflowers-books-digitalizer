# EvilFlowers Books Digitalizer

Digitalization pipeline for scanned books: TIFF scans → searchable **PDF/A**
(Slovak OCR), enriched with metadata and covers, then imported into the
[EvilFlowers Catalog](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog).
Built entirely on OSS tooling (Tesseract, OCRmyPDF, ScanTailor, img2pdf, pikepdf,
webdav4).

Planned follow-ups on the same pipeline: embeddings for a vector database and
richer classification for a graph database.

## Architecture

Scans are **two-page spreads on a black scanner bed** (uncompressed RGB TIFF @ 300
DPI, ~24 MB/frame). Frames are read by a pluggable **source** (a local mount in
production, WebDAV in dev — `[source]` in `configs/pipeline.toml`). Two engines
exist, selected by `[pipeline] engine`:

**`scantailor_mrc`** (production default — uniform page sizes with real margins,
no bleed-through, ~5× smaller PDFs):

```
source (TIFF frames per book)                          output/<source>/<slug>.{pdf,txt,cover.jpg,entry.json}
        │                                                                    ▲
        ▼                                                                    │
  DownloadBook ─► ScanTailorScans ─► [DocResEnhance] ─► DetectLanguage ─► MrcPdf
  (stage to       (split at gutter,   (optional AI        (quick OCR +     (Tesseract -> hOCR;
   local cache)    deskew, dewarp,     appearance pass)    langdetect)      recode_pdf MRC PDF
                   margins, uniform)                                        + text sidecar)
                                                                                  │
                                                                                  ▼
                 AttachMetadata ─► EnrichPdfMetadata ─► GenerateCover ─► FinalizePdf ─► WriteCatalogManifest
                 (Excel catalog)    (XMP: title,         (cover           (bookmarks,    (entry.json
                                     authors, year)      thumbnail)        page labels)   sidecar)
```

**`legacy`** (OpenCV preprocess → img2pdf → OCRmyPDF):

```
  DownloadBook ─► PreprocessScans ─► DetectLanguage ─► AssemblePdf ─► OcrPdf ─► EnrichPdfMetadata
                  (split spreads,                       (img2pdf,     (OCRmyPDF, PDF/A-2
                   crop bed, deskew)                     lossless)     + text sidecar)
```

Each step implements `PipelineStep.run(ctx: BookContext) -> BookContext`; a
`Pipeline` is just an ordered list of steps, so planned stages (classification,
embeddings on the OCR text sidecar) slot in without touching existing ones.
`pipeline.factory.build_pipeline` wires the engine + metadata/cover tail from
`configs/pipeline.toml`.

The empirical basis for the engine choice is in
[docs/digitalization_pipeline_report.md](docs/digitalization_pipeline_report.md).

## Setup

The pipeline shells out to system tools (Tesseract OCR, Ghostscript, and — for the
`scantailor_mrc` engine — `scantailor-deviant-cli` + `jbig2enc`). Install those for
your platform, then the Python package. Or skip the host install entirely and use
**[Docker](#docker-full-toolchain)**, which bakes in the full toolchain.

### 1. System dependencies

**macOS (Homebrew)**

```bash
brew install tesseract tesseract-lang ghostscript   # required
brew install jbig2enc                               # scantailor_mrc: MRC text mask
brew install unpaper pngquant                       # legacy engine
```

**Ubuntu / Debian (apt)**

```bash
sudo apt update && sudo apt install -y \
  tesseract-ocr tesseract-ocr-slk tesseract-ocr-ces tesseract-ocr-eng \
  ghostscript jbig2enc \
  unpaper pngquant                                  # legacy engine
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

> Heads-up: Ghostscript ≥ 10.6 has a known JPEG-encoding bug affecting PDF/A
> conversion; OCRmyPDF mitigates it, but visually check outputs (notebook 03,
> "pixel peeping") after Ghostscript upgrades.

## Notebooks

Experimentation happens in notebooks first; the module holds the reusable parts.
Install the notebook dependencies (`poetry install --with notebooks`), then
`poetry run jupyter lab`.

| Notebook                                   | Purpose                                                                 |
|--------------------------------------------|-------------------------------------------------------------------------|
| `01_webdav_exploration.ipynb`              | Inventory the shares: books, pages, sizes (interactive Plotly charts)   |
| `02_single_book_pipeline.ipynb`            | Download one book and run the standard pipeline end to end              |
| `03_transformation_lab.ipynb`              | Tune TIFF → PDF/A: spread splitting, compression/quality, OCR confidence |
| `04_produce_samples.ipynb`                 | Run the production pipeline on one small book per faculty               |
| `05_batch_pipeline.ipynb`                  | Batch: 4 books in parallel, low-storage mode, resumable                 |
| `06_scantailor_mrc_lab.ipynb`              | ScanTailor + MRC engine lab; compare against legacy output              |
| `07_fonts_tables_diagrams.ipynb`           | Variant comparison: font quality at 3× zoom, table/diagram survival     |
| `08_perfect_quality_lab.ipynb`             | Max-quality experiments (Sauvola/Wolf masks, facsimile MRC, roadmap)    |
| `09_finalizer_pero_pilot.ipynb`            | Finalizer + PERO/UVDoc/NAF-DPM pilots and the V2–V5 decision            |
| `10_metadata_catalog_draft.ipynb`          | Draft the librarian metadata Excel — one pre-filled row per book        |
| `11_local_e2e_experiment.ipynb`            | Local end-to-end test on a few small books (WebDAV source)              |
| `12_stats_and_results.ipynb`               | Stats & results dashboard — coverage, throughput, sample outputs        |

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
