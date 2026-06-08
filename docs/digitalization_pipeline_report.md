# A Reproducible Open-Source Pipeline for Mass Digitalization of Slovak Academic Books: Engineering Choices, Empirical Evaluation, and the Crisp-versus-Facsimile Trade-off

**Author.** Jakub Dubec
**Affiliation.** EvilFlowers Project
**Contact.** jdubec@culturepulse.ai
**Draft date.** 2026-06-07
**Status.** Working draft for academic publication — figures and single-corpus results are reproducible from the accompanying repository; see the Reproducibility appendix.

---

## Abstract

We present the design and empirical evaluation of an open-source pipeline that converts
uncompressed flatbed book scans into compact, searchable, navigable PDF documents. The
target corpus is a collection of approximately 880 Slovak academic books (two-page
spreads scanned on a black scanner bed as RGB TIFF at 300 DPI, ≈24 MB per frame). The
pipeline is built entirely from open-source components — Tesseract, OCRmyPDF, ScanTailor,
the Internet Archive's Mixed Raster Content (MRC) tooling, and pikepdf — and is organized
as a sequence of composable steps so that experimental and production configurations share
one code path. We contribute: (i) a comparison of two complete engines, a baseline
OpenCV → OCRmyPDF pipeline and a ScanTailor → MRC pipeline that produces uniformly
cropped pages roughly five times smaller while *increasing* recovered OCR word count;
(ii) a five-variant study isolating the effect of binarization mode, output resolution,
and AI appearance enhancement on font quality and the survival of tables, diagrams, and
colour; (iii) two carefully measured **negative-transfer** results, in which neural models
that are state-of-the-art on their own benchmarks (PERO-OCR for OCR, UVDoc for
geometric dewarping) *degrade* output on our corpus because their training distribution
(Czech newspapers; phone-camera-captured curled documents) does not match flatbed Slovak
book scans; (iv) a fail-safe "reading-experience" finalization stage that derives an
outline, printed-page labels, and a web-optimized byte layout from OCR structure; and
(v) an analysis of the strategic choice between a *crisp digital edition* and a *true
facsimile*, which we argue is a false binary best resolved by per-page content routing.
Our central methodological finding is that for a homogeneous flatbed corpus, classical,
well-tuned tooling is difficult to beat with general-purpose neural models trained on a
different capture modality; gains should be sought instead through per-page routing and
modality-matched models. All experiments are reproducible from cached artifacts.

**Keywords:** document image analysis, mass digitization, OCR, binarization, Mixed Raster
Content, MRC, JBIG2, dewarping, negative transfer, digital libraries, Slovak language.

---

## 1. Introduction

Mass digitization of academic and cultural-heritage books is a well-established practice,
but the engineering decisions that determine the *quality, size, and usability* of the
resulting documents are rarely reported with their supporting measurements. Practitioners
face a sequence of consequential choices — how to segment spreads, whether and how to
binarize, what compression model to use, which OCR engine and language model, and whether
to preserve a faithful tonal facsimile or to produce a compact "born-digital-feeling"
edition — and these choices interact. A choice that is correct for plain text can be
actively harmful for a colour cover or a continuous-tone plate.

This paper documents the design of an open-source pipeline for a concrete, homogeneous
corpus: roughly 880 Slovak academic books held on a networked share, scanned as two-page
spreads on a black scanner bed and stored as uncompressed RGB TIFF at 300 DPI
(≈24 MB per frame). The homogeneity of the corpus — a single capture modality, a single
dominant language (Slovak, with occasional English), a predominance of text and line-art
over photographs — is both an opportunity and a trap. It is an opportunity because a
well-tuned classical pipeline can exploit the regularities of the source. It is a trap
because it tempts the integration of high-profile neural models whose published strengths
were established on a *different* distribution.

We make five contributions:

1. **Two complete engines, measured against each other** (§4–§5.2). A baseline
   OpenCV → OCRmyPDF engine and a ScanTailor → MRC engine. On a representative book the
   MRC engine produces uniformly cropped pages, eliminates bleed-through, reduces file
   size roughly five-fold, and *increases* the recovered OCR word count.

2. **A five-variant ablation** (§5.3) isolating binarization mode, output DPI, and an
   optional AI appearance pass, and reporting their effect on font sharpness and on the
   survival of tables, diagrams, and colour.

3. **Two negative-transfer results** (§5.5, §5.6). PERO-OCR and UVDoc — each a leader on
   its own benchmark — measurably *underperform* the classical tooling on our corpus, and
   we trace each failure to a specific training-distribution mismatch.

4. **A fail-safe reading-experience finalizer** (§5.7) that derives a navigation outline,
   printed-page labels, and a linearized byte layout from OCR structure, degrading
   gracefully whenever its heuristics lack support.

5. **An analysis of the crisp-versus-facsimile fork** (§5.8, §6) showing that, for a
   text-and-line-art book, a facsimile edition's ~8× size premium buys correctness on a
   single page (the colour cover), and arguing for per-page routing rather than a
   corpus-wide commitment.

We are explicit about scope (see §7): most fine-grained measurements were taken on a
single, representative book (referred to throughout as the *fad sample*). The systems
contributions and the qualitative loss modes generalize; the precise scalar values should
be read as illustrative until the corpus-wide study described in §8 is completed.

---

## 2. Related Work and Background

**Searchable PDF production.** OCRmyPDF [1] orchestrates Tesseract [2] to add a text layer
to image PDFs and to produce PDF/A archival output. It is the backbone of our baseline
engine. Tesseract's LSTM engine and its language packs — in particular the Slovak (`slk`)
traineddata — are central to OCR quality for our corpus.

**Page cleanup and segmentation.** ScanTailor and its actively maintained *Deviant* fork
provide content detection, deskewing, dewarping, illumination normalization, margin
control, and binarization. We drive it through its command-line interface. Its `mixed`
mode binarizes text while preserving picture zones, and its `color_grayscale` mode keeps
tones everywhere.

**Compression via Mixed Raster Content.** The MRC imaging model (ITU-T T.44) decomposes a
page into a binary foreground *mask*, a foreground colour layer, and a background layer,
each compressed with a codec suited to its statistics — typically JBIG2 [3] for the bilevel
mask and JPEG 2000 [4] for the tonal layers. The Internet Archive's `archive-pdf-tools`
(`recode_pdf`) [5] is a production implementation that assembles such PDFs from page images
and an hOCR text layer; we adopt it directly.

**Binarization.** Adaptive thresholding methods — notably Sauvola [6] and Wolf [7] — remain
standard baselines for document binarization, and modern implementations (e.g. Doxa) make
them trivially available. Recent diffusion-based methods such as NAF-DPM [8] report
state-of-the-art results on the DIBCO benchmark series.

**Appearance enhancement and geometric correction.** DocRes [9] is a multi-task restoration
model (appearance, dewarping, deshadowing, deblurring). UVDoc [10] is a strong document
*unwarping* model evaluated on the DocUNet and UVDoc benchmarks, trained primarily on
camera-captured, physically curled documents.

**Language-specific OCR.** PERO-OCR [11], developed at the Brno University of Technology,
provides a layout analyser (ParseNet) and an OCR engine with publicly released European /
Czech-print models, plus a hosted service exposing newer engines.

**Vectorization.** `potrace` [12] traces a bilevel raster into smooth Bézier curves; applied
to a high-resolution text mask it is the open-source analogue of "ClearScan"-style glyph
vectorization.

Our work is less a new algorithm than a *systems-and-evaluation* study: which of these
components to combine, in what order, with what settings, for a specific corpus, and — just
as importantly — which high-profile components to *reject* and why.

---

## 3. System Architecture

### 3.1 Pipeline framework

The pipeline is a thin, explicit framework. A `BookContext` dataclass carries mutable
state through the run: the downloaded page images (`tiffs`), a dictionary of named output
artifacts (`raw_pdf`, `pdf`, `text`, `hocr`, …), and an accumulating `metadata`
dictionary. Each processing stage implements a single method,
`PipelineStep.run(ctx) -> ctx`, and a `Pipeline` is an ordered list of steps with per-step
timing and logging. New stages — planned embedding and graph-classification steps — slot
in without modifying existing ones.

Configuration is externalized to a single TOML file (`configs/pipeline.toml`), which is
the contract between the experimentation notebooks that *write* settings and the batch
runner that *consumes* them; changing behaviour does not require code changes. (TOML was
chosen over YAML deliberately, for its unambiguous typing and the absence of significant
whitespace.)

### 3.2 Two engines

The `[pipeline] engine` key selects one of two complete engines.

**Legacy engine** (`legacy`):

```
DownloadBook → PreprocessScans → DetectLanguage → AssemblePdf → OcrPdf → EnrichPdfMetadata
```

`PreprocessScans` uses OpenCV to split spreads, crop the scanner bed, trim binding
shadows, deskew, and white-balance the paper; `AssemblePdf` packs the cleaned frames
losslessly with img2pdf; `OcrPdf` runs OCRmyPDF (Tesseract) to produce a PDF/A-2 with a
text sidecar, with pngquant/JBIG2 optimization.

**ScanTailor + MRC engine** (`scantailor_mrc`, production default since 2026-06-07):

```
DownloadBook → ScanTailorScans → [DocResEnhance] → DetectLanguage → MrcPdf → EnrichPdfMetadata → FinalizePdf
```

`ScanTailorScans` performs content detection, deskew, dewarp, illumination normalization,
margin control, and produces uniform page sizes; an optional `DocResEnhance` applies an AI
appearance pass for tone-bearing pages; `MrcPdf` runs Tesseract to hOCR and then
`recode_pdf` to assemble an MRC PDF (JBIG2 mask over JPEG 2000 layers) with a text sidecar;
`FinalizePdf` (this work, §5.7) adds navigation and a web-optimized byte layout.

### 3.3 Engineering hazards discovered

Three non-obvious hazards were found during development and are encoded as defensive code,
because they silently corrupt output if ignored:

1. **ScanTailor CLI split bug.** With `--layout=2`, the detected gutter is not propagated
   to content detection: the right page receives the whole spread and the left page comes
   out blank. The workaround is to pre-split each spread at the gutter (via an
   `analyze_spread` routine) and run ScanTailor with `--layout=1`.

2. **Leptonica/Tesseract path resolution on macOS.** Tools fail on paths that traverse the
   macOS `/tmp` symlink; resolving to the canonical `/private/tmp` form is mandatory and is
   enforced in the MRC step.

3. **`recode_pdf` input globbing.** `recode_pdf` consumes a filesystem glob; stray images
   adjacent to the intended page set silently enter the document. The MRC step validates
   that the page set exactly matches the directory glob before invoking the tool.

These are reported because they are exactly the kind of defect that does not appear in a
unit test but reliably damages a months-long batch.

---

## 4. Methodology: the MRC Production Path

The production engine's quality rests on the interaction of two stages.

**Stage 1 — ScanTailor cleanup.** Spreads are split at the gutter and each half is processed
with content detection (which removes facing-page slivers and binding shadows), deskewing,
auto dewarping, illumination normalization, cautious despeckling, an 8 mm margin, and a
chosen colour mode. Critically, output is rendered at a *supersampled* resolution
(`output_dpi = 600` from 300 DPI input): the binarization is computed at 600 DPI so glyph
contours are smooth, even though the source is 300 DPI (see §5.3).

**Stage 2 — MRC assembly.** Tesseract OCRs the cleaned pages into a multi-page hOCR file,
from which a plain-text sidecar is extracted (the input for downstream enrichment and
classification). `recode_pdf` then assembles the MRC PDF: a JBIG2-compressed 1-bit text
mask is overlaid on smoothed, downsampled JPEG 2000 foreground/background layers. Text
remains razor-sharp because it lives in the lossless mask, while residual show-through is
visually suppressed by the smoothed background. A `--bg-downsample` control trades
background fidelity against size and is the single knob that distinguishes the compact and
facsimile profiles (§5.8).

The colour-mode choice has a known consequence that motivates the routing discussion in
§6: `mixed` mode kills bleed-through and yields the smallest files but binarizes stamps,
covers, and colour figures; `color_grayscale` mode (optionally paired with the DocRes
appearance pass) preserves tones everywhere at a size and compute cost.

---

## 5. Experiments and Results

Unless otherwise stated, fine-grained measurements were taken on a single representative
book, the *fad sample* (an architecture textbook of 41 produced pages, predominantly text
and line-art with one colour cover). §7 discusses the implications of this scope.

### 5.1 Tuning the baseline (legacy) engine

Iterating on the legacy engine established the value of several preprocessing choices,
measured by Tesseract's mean word confidence and per-page size:

- **Spread splitting** raised mean OCR confidence from 83 to 91 and reduced size from 0.64
  to 0.29 MB/page — splitting is unambiguously beneficial.
- **Greyscale retention** cost ≈3.5 confidence points for ≈4% size savings — i.e.
  retaining grey is slightly worse for OCR and not justified on size grounds alone.
- **Optimization level 2** (engaging pngquant, and JBIG2 where available) had no measurable
  downside.
- **Per-channel white balancing** mapped yellowed paper to white and improved legibility.

These results are unremarkable individually but collectively define a sensible baseline and
justify the defaults retained for the legacy engine.

### 5.2 The ScanTailor + MRC engine versus the baseline

On the fad sample, replacing the legacy engine with the ScanTailor + MRC engine produced:

- **Uniform page geometry with real margins** (versus the legacy engine's variable crops),
- **Elimination of bleed-through**,
- **A reduction in PDF size from 10.1 MB to 1.8 MB** (≈5×), and
- **An *increase* in recovered OCR words from 6364 to 6725.**

The simultaneous improvement in size *and* OCR recall is the key result: the MRC model is
not trading legibility for compactness; the cleaner, deskewed, uniformly binarized input
both compresses better and OCRs better. This engine was promoted to production default.

### 5.3 Variant study: binarization mode, resolution, and appearance

Five variants of the same book were built to isolate the effect of three settings
(ScanTailor colour mode, output DPI, and the DocRes appearance pass). Final document sizes:

| Variant | ScanTailor mode | Output DPI | DocRes | Size (MB) | Verdict |
|---|---|---|---|---|---|
| **V1** | `mixed` | 300 | – | 1.78 | Rejected — jagged fonts |
| **V2** | `mixed` | **600** | – | 3.25 | **Production default for text** |
| **V3** | `color_grayscale` | 600 | – | 4.14 | Anti-aliased but pale |
| **V4** | `color_grayscale` | 600 | ✓ | 4.14 | Print-like, +GPU minutes/page |
| **V5** | facsimile (`--bg-downsample 1`) | 600 | – | 25.44 | Faithful tones, 8× size |

Three findings:

- **The "jagged font" artifact of the first prototype (V1) was a resolution artifact, not
  an MRC/JBIG2 artifact.** Computing the *same* binarization at 600 DPI (V2) yields smooth
  contours. This corrected an early misattribution and is why `output_dpi = 600` is the
  default.
- **Tables and ruled content survive in all variants;** no special handling is required for
  ruled tables.
- **Two loss modes exist, both in `mixed` mode and both avoidable:** (a) content detection
  occasionally clips full-width figures (mitigated by routing the page to greyscale), and
  (b) colour elements are turned black by binarization (the dominant motivation for the
  routing strategy in §6).

### 5.4 The binarization ceiling: classical versus neural

To test whether classical binarization had been exhausted, Sauvola and Wolf thresholding
were applied to 2× (600 DPI) and 4× (1200 DPI) bicubically upsampled greyscale pages and
compared against ScanTailor's native supersampled binarization at 600 DPI. At reading zoom
all were comparable; the 1200 DPI Sauvola mask was marginally the smoothest. The conclusion
is that **bicubic interpolation adds no new stroke information** — further mask gains
require genuine super-resolution (i.e. a neural model that hallucinates plausible
high-frequency detail), not interpolation. A `potrace` demonstration on a 1200 DPI mask
confirmed that vector glyphs are the eventual quality ceiling, smooth at any zoom, at the
cost of a glyph-clustering pipeline not yet built.

### 5.5 OCR: Tesseract `slk` versus PERO-OCR (a negative-transfer result)

We piloted PERO-OCR's publicly released `pero_eu_cz_print_newspapers_2022-09-26` model
self-hosted on CPU. Setup was straightforward (a Python virtual environment, a 347 MB
model archive shipping CPU model variants and a CPU config; ≈2 s/page after a one-time
engine load). Layout detection was excellent. On Slovak text quality, however, the model
systematically failed on the Slovak-specific diacritics that do **not** occur in Czech
(`ľ ŕ ĺ ô ä`, and especially `ť`), while matching Tesseract exactly on diacritics the two
languages share (`č š ž ň ď`). Counts on three text-heavy pages:

| Page | Engine | `ľŕĺôä` (Slovak-only) | `ť` | `čšžňď` (shared) | chars |
|---|---|---|---|---|---|
| 5 | PERO | 6 | 1 | 30 | 1783 |
| 5 | Tesseract `slk` | 22 | 12 | 28 | 1738 |
| 8 | PERO | 4 | 0 | 26 | 1329 |
| 8 | Tesseract `slk` | 7 | 5 | 26 | 1336 |
| 15 | PERO | 4 | 0 | 21 | 1093 |
| 15 | Tesseract `slk` | 7 | 7 | 21 | 1099 |

Read side by side, PERO renders `Cielom`, `vytvárat`, `slúžit` where Tesseract correctly
reads `Cieľom`, `vytvárať`, `slúžiť`. The model's character set *contains* every Slovak
glyph (verified directly in its engine definition), but the Czech-newspaper *training
distribution* means it rarely *predicts* the Slovak-only letters: it normalizes Slovak
orthography toward Czech. **Charset coverage is not language coverage.** PERO additionally
scrambled two-column reading order on one page where Tesseract preserved it. We therefore
retain Tesseract `slk`. The newer engines on PERO's hosted service may differ and remain a
candidate for a future, manually-registered benchmark.

### 5.6 Geometric correction: ScanTailor versus UVDoc (a second negative-transfer result)

We piloted UVDoc, a strong document-unwarping model whose weights ship in-repository. Its
demo hard-codes a CUDA load; a short CPU/MPS-safe loader was sufficient to run it at
≈0.3 s/page on Apple-Silicon MPS. We selected the five most-curved fad pages by a baseline
heuristic and measured **per-text-line vertical bow** (the peak-to-peak deviation of each
detected line's ink centroid from a straight-line fit, median over lines; lower is
straighter) for the raw scan, ScanTailor `dewarping=auto`, and UVDoc:

| Page | Original (px) | ScanTailor (px) | UVDoc (px) |
|---|---|---|---|
| 9 | 17.7 | **16.8** | 21.0 |
| 21 | 20.0 | **18.5** | 25.2 |
| 26 | **6.1** | 14.2 | 11.8 |
| 36 (figure spread) | **12.6** | 24.1 | 28.1 |
| 37 | **10.3** | 19.7 | 18.3 |

**UVDoc never wins** — it is worst on four of five pages and second on the fifth — and a
visual overlay against straight reference lines shows it *skewing* an already-flat page and
re-introducing the scanner-bed border that ScanTailor crops. The explanation is again
distributional: these flatbed scans are already nearly flat (even the most-curved page bows
under 1% of its height), and UVDoc, trained on phone-camera captures of physically curled
documents, hallucinates a curl and bows the straight lines while "correcting" it. We do not
integrate UVDoc for this corpus; it would earn its place only on a sub-corpus that is in
fact camera-captured with real page curl.

### 5.7 Reading-experience finalization

We added a `FinalizePdf` stage that derives three usability improvements from existing
artifacts, each fail-safe:

1. **Outline bookmarks** from the Tesseract hOCR. A line is treated as a heading when its
   font size (`x_size`) is at least 1.18× the body-text median *and* it survives a battery
   of noise filters (≤6 words; ≥78% alphabetic; begins with an uppercase letter or digit;
   no trailing hyphen or bare number; no table-ruling characters; no lone capital inside a
   mixed-case line). On the fad sample this yielded 14 bookmarks — 10 true section headings
   plus 4 genuine title-page lines — with worksheet noise correctly excluded.

2. **Printed-page labels** (`/PageLabels`) from page numbers detected in the top/bottom 8%
   margin band, fitted to a modal `printed − physical` offset and applied only when at least
   three pages agree (front matter rendered as lowercase roman). On the fad sample, which
   has no printed numbers, this was correctly skipped.

3. **Linearization** (web-optimized byte layout) via pikepdf, validated with `qpdf --check`.

Each piece degrades gracefully: absent hOCR or insufficient agreement simply logs and
continues, so the stage can never fail a batch over a heuristic. The finalized fad document
remained 1.78 MB.

### 5.8 The crisp-versus-facsimile trade-off

The strategic question framed by the variant study is whether to ship a **crisp digital
edition** (V2 — text/line-art binarized into a lossless mask, 3.25 MB) or a **true
facsimile** (V5 — all tones preserved, 25.44 MB, a 7.8× premium). We rendered the same
three pages from both — plain text, a line-art worksheet, and the colour cover — and
classified all 41 pages by content type (colourfulness and midtone fraction):

| Content | V2 crisp (3.25 MB) | V5 facsimile (25.4 MB) | Outcome |
|---|---|---|---|
| Plain text | crisp 1-bit | identical | Tie — V5 wastes bytes |
| Line-art diagrams | clean | identical | Tie — line-art binarizes perfectly |
| Continuous tone | lost | preserved | V5 (none present in this book) |
| Colour cover | binarized to a **solid black block** | colour preserved | V5, decisively |

Of 41 pages, 29 are text/line-art and 11 are faint scan haze on line-art that is *visually
identical* between the two profiles; **exactly one page — the colour cover — genuinely
differs.** On that page V2 does not merely flatten the cover; it renders the blue artwork
as an ugly solid black rectangle. For a text-and-line-art book, therefore, the facsimile
profile's ~8× size premium buys correctness on a single page.

---

## 6. Discussion

### 6.1 A unifying theme: negative transfer from mismatched capture modality

The two neural pilots (§5.5, §5.6) failed for the *same structural reason*, not for
idiosyncratic ones. PERO-OCR was trained on Czech newspapers and normalizes Slovak toward
Czech orthography; UVDoc was trained on phone-camera captures of curled pages and
hallucinates curl on flat scans. In both cases the architecture is sound and the model is
genuinely strong *on its own benchmark*; the failure lives entirely in the gap between the
training distribution and our corpus. The practical lesson for digitization projects is
specific: **the headline benchmark number of a neural component is not evidence for your
corpus unless the capture modality and language match.** A modality-matched smaller model,
or a well-tuned classical method, is frequently the better engineering choice — and the
only way to know is to measure on the actual corpus, as we did.

### 6.2 The product fork is a false binary: route by content

The crisp-versus-facsimile decision (§5.8) should not be made corpus-wide. The right
architecture is per-page routing, for which the pipeline is already positioned (the figure
detection heuristics of §5.3 and an existing `is_color_page` predicate):

1. **Default to the crisp profile** for the document body (text and line-art, the large
   majority of pages, visually identical to facsimile at a fraction of the size).
2. **Route colour/photo pages** through the tone-preserving path so that covers and plates
   remain correct *inside* an otherwise-compact PDF.
3. **Offer a full facsimile only as a premium edition** for the rare book that is
   predominantly continuous-tone (illustrated histories, art catalogues), where a global
   facsimile actually earns its size.

This reframes a strategic commitment as a per-page (and per-book) classification problem.
The single open input is the *fraction of the corpus that is photo/colour-heavy*, which is
measurable with the same `is_color_page` pass over the source TIFFs and which sizes the
premium tier.

### 6.3 Where quality gains remain

Within the crisp path, the remaining ceiling is vectorization: clustering glyph instances
and tracing them to embedded vector fonts (the `potrace` demonstration of §5.4). Within the
facsimile path, a modality-matched neural binarization model (e.g. NAF-DPM, §8) could
sharpen masks beyond the interpolation ceiling — but only if its weights and device support
can be obtained.

---

## 7. Limitations and Threats to Validity

- **Single-book quantitative scope.** Most scalar results were measured on one
  representative book. The systems contributions (the engine design, the discovered
  hazards, the finalizer) and the *qualitative* loss/transfer modes generalize, but the
  precise numbers (sizes, confidence deltas, diacritic counts, bow values) should be treated
  as illustrative until the corpus-wide study of §8 is run. This is the most important
  caveat and the first a reviewer should weigh.

- **OCR ground truth.** §5.5 uses Tesseract's own output as the reference for Slovak
  diacritics rather than a human-verified transcription. Because the failing tokens are
  high-frequency Slovak function words with unambiguous correct spellings (`Cieľom`,
  `vytvárať`), the *direction* of the result is not in doubt, but a formal character/word
  error rate against gold transcriptions is needed for publication-grade precision.

- **Heuristic metrics.** The "per-line bow" metric (§5.6) and the page-content classifier
  (§5.8) are bespoke proxies. They are adequate to support the qualitative conclusions
  (which are corroborated by visual inspection) but are not validated instruments.

- **Tool and platform specificity.** Results were produced on macOS / Apple Silicon with
  specific tool versions; one known Ghostscript JPEG-encoding regression affecting PDF/A
  conversion is mitigated by OCRmyPDF but warrants visual checks after upgrades.

- **Neural pilots used public checkpoints.** PERO-OCR's hosted service and any newer UVDoc
  or DocRes checkpoints were not evaluated; conclusions apply to the specific public models
  tested.

---

## 8. Future Work

1. **Corpus-wide content survey.** Run the `is_color_page`/figure heuristics over all ≈880
   books to quantify the photo/colour-heavy fraction, validating §5.8 at scale and sizing
   the premium-facsimile tier.

2. **Per-page routing implementation.** Realize the hybrid of §6.2 in the pipeline,
   directing colour/photo pages to the tone path within an otherwise-crisp document.

3. **Gold-standard OCR evaluation.** Produce human-verified transcriptions for a stratified
   page sample and report CER/WER for Tesseract `slk`, PERO's hosted engines, and any
   Slovak-aware post-OCR corrector.

4. **Modality-matched neural binarization.** Pilot NAF-DPM once its DIBCO binarization
   checkpoint is obtained (currently access-gated); the architecture (a lightweight backbone
   with a few-step solver) is CPU/MPS-friendly and the only expected port is device
   selection.

5. **Glyph vectorization at scale.** Build the glyph-clustering → `potrace` → embedded-font
   pipeline suggested by §5.4 to reach the vector-quality ceiling for the crisp edition.

6. **Downstream enrichment.** Use the OCR text sidecars for embeddings (vector database) and
   richer classification (graph database), the originally planned follow-ups.

---

## 9. Conclusion

We have described an open-source pipeline for mass digitization of a homogeneous Slovak
academic-book corpus and evaluated its principal engineering choices. A ScanTailor + MRC
engine improves on a tuned OpenCV → OCRmyPDF baseline on every axis that matters
simultaneously — geometry, bleed-through, size, and OCR recall — and is our production
default. A five-variant ablation shows that font quality is governed by binarization
*resolution*, not by the MRC model, and that the only systematic loss modes are confined to
colour/photo content under `mixed` binarization. Two high-profile neural models that lead
their own benchmarks (PERO-OCR, UVDoc) measurably degrade our output, each because of a
training-distribution mismatch, yielding a transferable caution: benchmark leadership does
not transfer across capture modality or language. Finally, the strategic
crisp-versus-facsimile choice is best dissolved into per-page content routing, with a full
facsimile reserved as a premium tier. The broad lesson is that for a homogeneous corpus,
careful measurement on the *actual* data, classical tooling tuned to the source, and
content-aware routing outperform the reflexive adoption of state-of-the-art neural
components.

---

## Reproducibility Appendix

All experiments are reproducible from the project repository. The relevant artifacts:

- **Pipeline code.** `evilflowers_books_digitalizer/pipeline/` — framework (`base.py`),
  engine assembly (`factory.py`), and steps (`steps/`), including the `MrcPdf` and
  `FinalizePdf` stages discussed here. Configuration in `configs/pipeline.toml`.
- **Experiment notebooks.** `notebooks/06`–`09`: the MRC engine lab, the variant/font/table
  study, the max-quality experiments (Sauvola/Wolf, V5 facsimile, potrace), and the roadmap
  execution (finalizer, PERO, UVDoc, NAF-DPM, and the V2–V5 decision). Notebook 09 is
  re-executable end-to-end and regenerates the figures referenced in §5.5–§5.8.
- **Prototype documents.** `output/prototypes/fad_V1`–`V5*.pdf` — the five variants of
  §5.3/§5.8.
- **Comparison figures.** `output/lab09/v2_v5_pages.png`, `output/lab09/v2_v5_detail.png`
  (§5.8), and the UVDoc overlay (§5.6).
- **External tooling** (host-local, not redistributed): `scantailor-deviant-cli`; DocRes,
  PERO-OCR, and UVDoc environments under `~/.local/share/evilflowers-tools/`. A Dockerfile
  reproduces the core toolchain.

Note on data availability: the source scans reside on a private networked share and are
not redistributable; the cached intermediate artifacts and produced PDFs needed to
reproduce the reported figures are retained in `output/`.

---

## References

> Bibliographic details below should be verified and completed to the target venue's style
> before submission; URLs are provided for the software components.

[1] OCRmyPDF. https://github.com/ocrmypdf/OCRmyPDF
[2] R. Smith. "An Overview of the Tesseract OCR Engine." *ICDAR*, 2007.
    Tesseract OCR: https://github.com/tesseract-ocr/tesseract
[3] ITU-T Recommendation T.88 (JBIG2); P. G. Howard et al., "The emerging JBIG2 standard,"
    *IEEE Trans. Circuits Syst. Video Technol.*, 1998.
[4] ITU-T Recommendation T.800 / ISO/IEC 15444 (JPEG 2000).
[5] Internet Archive, `archive-pdf-tools` (`recode_pdf`).
    https://github.com/internetarchive/archive-pdf-tools
[6] J. Sauvola and M. Pietikäinen. "Adaptive document image binarization."
    *Pattern Recognition*, 33(2):225–236, 2000.
[7] C. Wolf and J.-M. Jolion. "Extraction and recognition of artificial text in multimedia
    documents." *Pattern Analysis and Applications*, 6(4):309–326, 2004.
[8] G. Cicchetti et al. "NAF-DPM: A Nonlinear Activation-Free Diffusion Probabilistic Model
    for Document Enhancement." arXiv:2404.05669, 2024.
    https://github.com/ispamm/NAF-DPM
[9] J. Zhang et al. "DocRes: A Generalist Model Toward Unifying Document Image Restoration
    Tasks." *CVPR*, 2024. https://github.com/ZZZHANG-jx/DocRes
[10] F. Verhoeven, T. Magne, O. Sorkine-Hornung. "UVDoc: Neural Grid-based Document
    Unwarping." *SIGGRAPH Asia*, 2023. https://github.com/tanguymagne/UVDoc
[11] O. Kodym, M. Hradiš et al. PERO-OCR, Brno University of Technology.
    https://github.com/DCGM/pero-ocr ; https://pero-ocr.fit.vutbr.cz
[12] P. Selinger. "Potrace: a polygon-based tracing algorithm." 2003.
     https://potrace.sourceforge.net
[13] ScanTailor Deviant. https://github.com/ScanTailor-Advanced/scantailor-advanced
[14] ITU-T Recommendation T.44, Mixed Raster Content (MRC).
