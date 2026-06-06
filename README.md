# EvilFlowers Books Digitalizer

Digitalization pipeline for scanned books: TIFF scans on WebDAV shares →
searchable PDF/A documents (Slovak OCR), enriched with metadata and classified.
Built entirely on OSS tooling (Tesseract, OCRmyPDF, img2pdf, pikepdf, webdav4).

Planned follow-ups on the same pipeline: embeddings for a vector database and
richer classification for a graph database.

## Architecture

The scans are **two-page spreads on a black scanner bed** (uncompressed RGB
TIFF @ 300 DPI, ~24 MB/frame) — preprocessing splits, crops and deskews them
before OCR:

```
WebDAV (TIFF frames per book)                                       output/<source>/
        │                                                                 ▲
        ▼                                                                 │
  DownloadBook ─► PreprocessScans ─► DetectLanguage ─► AssemblePdf ─► OcrPdf ─► EnrichPdfMetadata
  (local cache)   (OpenCV: split      (quick OCR +      (img2pdf,     (OCRmyPDF, detected
                   spreads, crop bed,  langdetect ->     lossless)     language e.g. slk+eng,
                   trim binding        e.g. "eng+slk")                 PDF/A-2 + text sidecar,
                   shadows, deskew,                                    pngquant/jbig2 optimize)
                   whiten paper)
```

Settings live in `configs/pipeline.toml` (written by notebook 03, consumed by
`pipeline.factory.build_pipeline`). Classification (`ClassifyBook`, pluggable
`Classifier`) and embedding stages will run later on the OCR text sidecar.

Each step implements `PipelineStep.run(ctx: BookContext) -> BookContext`;
a `Pipeline` is just an ordered list of steps, so new stages (embeddings,
graph classification) slot in without touching existing ones.

## Setup

```bash
# System dependencies (macOS)
brew install tesseract tesseract-lang ghostscript   # required
brew install unpaper pngquant                       # scan cleaning + PNG optimization
brew install jbig2enc                               # recommended: JBIG2 for monochrome images

# Python
poetry install

# Credentials (gitignored)
cp credentials.example.toml credentials.toml        # then fill in WebDAV credentials
```

Tesseract must have the Slovak language pack (`tesseract --list-langs | grep slk`).

## Usage

Experimentation happens in notebooks first; the module holds the reusable parts.

| Notebook | Purpose |
|----------|---------|
| `notebooks/01_webdav_exploration.ipynb` | Inventory the shares: books, pages, sizes — interactive Plotly charts (cached to `.cache/stats/`) |
| `notebooks/02_single_book_pipeline.ipynb` | Download one book and run the standard pipeline end to end |
| `notebooks/03_transformation_lab.ipynb` | Tune the TIFF → PDF/A transformation: spread splitting, unpaper, compression/quality matrix, OCR confidence |
| `notebooks/04_produce_samples.ipynb` | Run the production pipeline on one small book per faculty |
| `notebooks/05_batch_pipeline.ipynb` | Batch run: 4 books in parallel (process pool), low-storage mode (cache deleted per book), resumable via `output/batch_report.jsonl` |

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
├── models.py             BookRef, PageScan, SourceStats
├── webdav.py             BookSource — listing, stats, resumable downloads
├── cache.py              LocalCache — scans & cached stats layout
└── pipeline/
    ├── base.py           BookContext, PipelineStep, Pipeline
    └── steps/            download, assemble, ocr, enrich, classify
notebooks/                exploration & experimentation
credentials.toml          WebDAV secrets (gitignored, see credentials.example.toml)
.cache/                   downloaded scans + share stats (gitignored)
output/                   produced PDFs/text (gitignored)
```
