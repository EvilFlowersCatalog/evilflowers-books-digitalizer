# EvilFlowers Books Digitalizer

Digitalization pipeline for scanned books: TIFF scans on WebDAV shares →
searchable PDF/A documents (Slovak OCR), enriched with metadata and classified.
Built entirely on OSS tooling (Tesseract, OCRmyPDF, img2pdf, pikepdf, webdav4).

Planned follow-ups on the same pipeline: embeddings for a vector database and
richer classification for a graph database.

## Architecture

The scans are **two-page spreads on a black scanner bed** (uncompressed RGB
TIFF @ 300 DPI, ~24 MB/frame). Two engines exist, selected by
`[pipeline] engine` in `configs/pipeline.toml`:

**`scantailor_mrc`** (production default since 2026-06-07, prototyped in
notebook 06 — uniform page sizes with real margins, no bleed-through, ~5×
smaller PDFs):

```
WebDAV (TIFF frames per book)                                          output/<source>/
        │                                                                    ▲
        ▼                                                                    │
  DownloadBook ─► ScanTailorScans ──► [DocResEnhance] ─► DetectLanguage ─► MrcPdf ─► EnrichPdfMetadata
  (local cache)   (split at gutter,    (optional AI       (quick OCR +     (Tesseract -> hOCR;
                   scantailor-cli:      appearance pass,    langdetect)     recode_pdf: JBIG2 text
                   content detection,   keeps stamp/photo                   mask over JPEG2000
                   deskew, dewarp,      tones; ~2 min/page)                 layers — MRC PDF
                   illumination,                                            + text sidecar)
                   margins, uniform
                   page size)
```

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
`pipeline.factory.build_pipeline`). Classification (`ClassifyBook`, pluggable
`Classifier`) and embedding stages will run later on the OCR text sidecar.

Each step implements `PipelineStep.run(ctx: BookContext) -> BookContext`;
a `Pipeline` is just an ordered list of steps, so new stages (embeddings,
graph classification) slot in without touching existing ones.

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

## Usage

Experimentation happens in notebooks first; the module holds the reusable parts.

| Notebook | Purpose |
|----------|---------|
| `notebooks/01_webdav_exploration.ipynb` | Inventory the shares: books, pages, sizes — interactive Plotly charts (cached to `.cache/stats/`) |
| `notebooks/02_single_book_pipeline.ipynb` | Download one book and run the standard pipeline end to end |
| `notebooks/03_transformation_lab.ipynb` | Tune the TIFF → PDF/A transformation: spread splitting, unpaper, compression/quality matrix, OCR confidence |
| `notebooks/04_produce_samples.ipynb` | Run the production pipeline on one small book per faculty |
| `notebooks/05_batch_pipeline.ipynb` | Batch run: 4 books in parallel (process pool), low-storage mode (cache deleted per book), resumable via `output/batch_report.jsonl` |
| `notebooks/06_scantailor_mrc_lab.ipynb` | ScanTailor + MRC engine lab: run one cached book through the new engine, eyeball pages, compare against legacy output |
| `notebooks/07_fonts_tables_diagrams.ipynb` | Variant comparison (mixed@300/600, grayscale, DocRes): font quality at 3× zoom, table/diagram survival checks, corpus inventory of figure-heavy books |
| `notebooks/08_perfect_quality_lab.ipynb` | Max-quality experiments: Sauvola/Wolf supersampled masks, facsimile-grade MRC profile (V5), potrace vector-glyph demo, prioritized roadmap (PERO-OCR, UVDoc, NAF-DPM, neural SR, smoothscan) |

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
