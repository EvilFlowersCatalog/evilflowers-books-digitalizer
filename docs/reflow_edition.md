# Toward a Born-Digital Edition: Reflowing Scanned Technical Books

**Status.** Experiment report, 2026-11-06. Reproducible from
`experiments/reflow/` (see its README). The code is not wired into the
pipeline yet; see §8 for the proposed integration.

**Question.** How close can the digitizer get to a document that looks
*born digital*, rather than a cleaned-up scan? If possible, the document should be
rebuilt from scratch, for example as a reflowable EPUB.

**Test book.** *Fyzika po kapitolách 1: Vektory* (Ivan Červeň, STU 2007,
ISBN 978-80-227-2663-4), `fei_CVI_OPACID_FEI_9788022726634`:

- 58 pages, of which 53 content pages were processed;
- a hard case: dense vector calculus, determinants, line-art figures, glossary,
  library stamps.

**Input caveat.** The raw-scan cache was gone, so every experiment ran on the cached
**legacy-engine** output. That is RGB JPEG at about 300 dpi, with bleed-through and a
facing-page sliver on some pages. §7 checks the analyzer on new-engine (ScanTailor,
600 dpi) pages.

All quality judgements below are **visual checks by the author, not measurements
against a ground-truth transcription**.

---

## 1. Summary

| | Variant | What it is | Size | Math | Reflowable |
|---|---|---|---|---|---|
| A0 | current | MRC/OCRmyPDF image PDF + invisible OCR text | 10.96 MB | image only; text layer is garbage on formulas | no |
| A1 | vector glyphs | A0's text mask traced with potrace | ≈ same per page | image | no |
| B | reflow, Tesseract only | EPUB from hOCR; formulas as image crops | 11.0 MB EPUB | 185 image crops | yes |
| **C** | **reflow, hybrid** | **Tesseract text + PaddleOCR-VL math, anchored** | **0.77 MB EPUB / 0.69 MB PDF** | **115 display formulas + 60 mixed lines as real math (MathML / LaTeX); 3 formula image fallbacks + 6 inline lines reverted to Tesseract text (9 VLM rejections in total)** | **yes** |

**EPUB validation.**

- Variant B was checked with epubcheck 5.4.0. It reported 6 errors, all
  `OPF-073`: potrace writes an SVG DOCTYPE. The fix is in the code.
- **Variant C has not been re-validated**, because the re-run was blocked in the
  session. Do not assume C passes until epubcheck (and ideally Ace) have run.

**Recommendation.** Ship **C as an additional, derived edition** (`<slug>.epub`).
Keep the MRC PDF as the faithful master. C is the only variant that reads like a
born-digital book: it reflows, its math is selectable and accessible (MathML), it is
about 15× smaller, and it can be re-typeset to PDF.

C still has visible residual errors (§6), so it is a *reading edition*, not a
replacement for the facsimile.

## 2. Literature and landscape (condensed)

**Full-page VLM parsers.** As of late 2026, the leaders on OmniDocBench v1.6 are
PaddleOCR-VL-1.6 (96.3), MinerU2.5-Pro (95.8) and GLM-OCR (95.2). These numbers
are self-reported or come from aggregator leaderboards; they were not reproduced
here.

- **PaddleOCR-VL** is the only one that is all of: Apache-2.0 code *and* weights,
  0.9B parameters (runs on a laptop), and with Slovak named in its language list.
- Marker/Chandra and Surya ship OpenRAIL-M weights with commercial restrictions.
- Granite-Docling and GLM-OCR do not cover Slovak.
- olmOCR-2 needs an NVIDIA GPU.

**Hallucination literature.** Small VLMs "rewrite" toward their language priors (ACL
2026 *Linguistic Priors Hallucination*; *Do VLMs Read or Rewrite?* 2026). These
errors are invisible to CER (*When Low CER is Not Enough*, 2026). LLM post-correction
gives no gain for lower-resource languages (*No Free Lunches*, 2025). Correction
works when it is anchored to a reference (AAAI 2025).

**ClearScan-style vector glyphs.** No mature open-source implementation exists.
ScanWeave is early-stage, and smoothscan has weak results.

**Practice.**

- Distributed Proofreaders and Wikisource rely on human page proofing.
- Standard Ebooks starts from existing transcriptions.
- EPUB accessibility (EPUB A11y 1.1, legally relevant since the EAA in June 2025)
  favours **reflowable** EPUB over fixed-layout page images.

**YoGaDoc** (an STU project) contributes one transferable idea: DocLayout-YOLO
(DocLayNet classes) as a trained layout detector. Its EasyOCR is weaker than
Tesseract `slk` for Slovak. DocLayout-YOLO is AGPL-3.0. It was not run (§9).

## 3. Architecture of variant C

```
page image + Tesseract slk hOCR
  └─ analyze: blocks (heading | para | list | table | formula | figure | caption)
       ├─ prose        -> Tesseract text (diacritic authority)
       ├─ 2-D math     -> PaddleOCR-VL "Formula Recognition:" -> LaTeX
       ├─ prose+math   -> PaddleOCR-VL "OCR:" -> anchored to Tesseract words
       ├─ tables       -> rebuilt from Tesseract word geometry (no VLM)
       └─ figures      -> crop, paper->white, line art traced to SVG (potrace)
  └─ guards -> Markdown -> pandoc: EPUB 3 (MathML) + xelatex PDF
```

**Routing is per line**, on Tesseract signals:

- **Low word confidence with few dictionary-like words** marks math. Prose scores
  93–96 and display math 38–43.
- **Inline-math characters, or Tesseract's misread-Greek signature (`@ © ®` for
  ω)**, mark a mixed line.

**Anchoring.** VLM prose tokens snap to Tesseract's high-confidence words. The
snapping is:

- **order-aware:** anchors are consumed left to right, within a look-ahead window;
- **diacritic- and script-insensitive:** `funkcίου`≡`funkciou`, `polá`≡`poľa`;
- **arbitrated by the Slovak hunspell dictionary:** if only the VLM word is a real
  word, it wins, as in `jednotkový`.

## 4. Negative result 1: a SOTA VLM on Slovak prose

Here is PaddleOCR-VL-1.6 `OCR:` on a clean prose block that Tesseract `slk`
reads perfectly. The excerpt is verbatim.

> …vektorovou funkc**ίου**. … súvislos**t** … **kł**adných … zdroje pol**á** … V magnetických
> pol**í**ach, kde indu**čně**néčiary sú … v **úbobovnom** bode … súradnicovej sústav

The output contains:

- Greek letters inside a Slovak word;
- Polish `ł` and Czech `ě`;
- dropped `ť`/`ľ`;
- two words merged into one;
- a truncated ending.

This **reproduces the 2026-06 PERO-OCR finding** with a different, newer model:
benchmark leadership does not transfer to Slovak prose. The VLM is therefore
**never trusted for prose**.

## 5. What works: math recovery

**Formulas.** On visual inspection (`experiments/reflow/formula_sheet_40.png`),
display formulas come out as correct LaTeX, including 3×3 determinants,
multi-line systems and long expansions. Typical results:

- `f=\sqrt{f_x^2+f_y^2+f_z^2}`
- the full `rot F` determinant expansion

**Anchoring on inline/mixed lines.** 136 segments, 480 anchors:

| text source | Slovak-only diacritics (ľŕĺôäť) | shared (čšžňď) | foreign letters |
|---|---|---|---|
| raw VLM | 15 | 88 | 20 |
| **anchored + dictionary** | **42** | 92 | 19 → **0 published** |
| Tesseract alone | 36 | 77 | 0 |

The "foreign letters" column is counted *before* the per-line guard. Any line that
still contains a non-Slovak letter after anchoring falls back to Tesseract. A scan of
the final Markdown finds **0** such letters in prose.

On these hard lines the hybrid recovers **more** Slovak-only diacritics than either
engine alone.

**Guards.**

| guard | count | what it caught |
|---|---|---|
| figure-shape pre-check | — | drawings routed to the formula model |
| hallucination post-check | 2 rejected | a diagram read as `\begin{array}` of its labels; caption text read as "math" |
| colour stamp removal | — | the round STU library seal (it had been read as "SKA TECH N I C K A") |
| per-snippet xelatex validation | 1 of 297 invalid | one malformed matrix that would have aborted the whole PDF |
| `\tag` lifted out of inline math | — | equation numbers that LaTeX forbids inside inline math |

## 6. Residual errors (visual check)

- **Rare, silent formula misreads.** Examples: `div` read as `\mathrm{l i v}`, and `∂`
  read as `λ` with an extra determinant row. They are syntactically valid, so no
  guard catches them. This is the main open risk for a physics text.
  - **Mitigation (future work):** a second formula recognizer as a cross-check, or
    render-and-compare against the crop.
- **Greek misreads in high-confidence prose lines are not routed.** Examples: `Ar` for
  `Δr`, `r»` for `r₂`.
- **Display equations sometimes typeset inline** (`dr/dt = lim…`). These are the
  mixed-line path.
- **Figure labels outside the detected box leak into the text.** Captions get a
  doubled prefix (`Obrázok 7: Obr. 1.3.2.1`).
- **The re-typeset PDF is not hyphenated.** TeX Live basic lacks the Slovak
  patterns.
- **Variant B is no smaller than A0.** Its formula crops are unoptimized PNGs.

## 7. Transfer to new-engine input (ScanTailor `mixed`, 600 dpi)

The analyzer ran on 600 dpi grey renders of the new-engine fad *distribution PDF*
(JPEG background plus JBIG2 mask). That is a proxy for ScanTailor output, not the
actual ScanTailor TIFFs, which were not cached.

- Tables (`nie / áno` value columns) and lettered lists classify correctly.
- **The running head is no longer removed.** ScanTailor margins push it below the
  10 % band. This needs book-level detection of repeated text.
- **Worksheet/form pages become noise.** This needs a whole-page image fallback.
- **Colour stamp removal cannot work on bitonal `mixed` output.** It would have to run
  before ScanTailor. Its production placement is still to be decided.
- fad has no math, so the math path is validated only on legacy input.

## 8. Vector glyphs (A1): cosmetic only

Tracing the 600 dpi JBIG2 mask with potrace gives 282–361 KiB of vector outlines
per page. At reading zoom the edges are smoother and specks vanish. But the glyph
bumps from the 300→600 dpi upsample are traced faithfully, and each `a` stays
slightly different (`experiments/reflow/raster_vs_vector.png`).

A born-digital *look* needs either glyph clustering (identical glyph instances, a
Type 3 font) or full re-typesetting. Glyph clustering is risky for Slovak diacritic
pairs (`l`/`ľ`, `c`/`č`).

## 9. Throughput and operations

| step | cost on M1 Max (MPS, HF transformers) |
|---|---|
| Tesseract | ≈ 2 s/page |
| PaddleOCR-VL | median 3.3 s per region, p95 13 s; ≈ 24 s/page on this math-dense book (1 293 s / 53 pages) |
| LaTeX validation | 6–10 s/book, cached |
| pandoc EPUB | < 1 s |
| xelatex PDF | ≈ 9–45 s |

Prose-only books need almost no VLM calls. Corpus-scale math needs the official
GGUF/vLLM serving on a GPU; HF `generate` on MPS runs at about 8 tok/s.

**Operational gotchas found.**

- Pathological crops (3 × 140 px) explode under the processor's min-pixel upscaling
  and OOM the GPU. Crops are now white-padded to aspect ≤ 10 and capped.
- MPS memory accumulates across `generate` calls, and `torch.mps.empty_cache()` does
  not release it. The driver's "other allocations" grow to about 40 GB until jobs OOM.
  In the first pipeline end-to-end run, 30 of 67 jobs failed this way. The runner
  therefore **recycles the worker process every 25 jobs** and retries an OOM'd job once
  in a fresh process (`reflow/vlm.py`: `JOBS_PER_PROCESS`, `MAX_ATTEMPTS`).
- The VLM result cache is content-addressed (page + bbox + task), so re-analysis
  never mis-attaches stale results.

## 10. Integration (2026-11-07)

The edition is now built in the pipeline by the opt-in step `ReflowEdition`
(`pipeline/steps/reflow.py`). The code lives in the `evilflowers_books_digitalizer/reflow/`
package:

| Module | Role |
|---|---|
| `model.py` | hOCR model |
| `analyze.py` | page analysis plus book-level running heads and form-page detection |
| `anchor.py` | anchoring and the Slovak lexicon |
| `vlm.py` | jobs, the content-addressed cache, hallucination and LaTeX guards |
| `vlm_worker.py` | standalone sidecar run in the tool venv |
| `edition.py` | Markdown writer and pandoc EPUB build |

**Pipeline placement.** The step runs after `EnsurePdfA`, while the cleaned pages are
still in the work dir. Its input is the pipeline's multi-page hOCR at 600 dpi. Its output
is `<slug>.epub`, plus a report in `metadata["reflow"]`. It is
fail-safe: an error is recorded, but the book is never failed.

**In the Restoration Studio.** The studio's renderer (`studio/adapters/render`) builds
`<id>.epub` with the same `reflow.edition.build_edition`, from the access leaves and the
book's hOCR. It does not yet use the studio's region decisions or VLM answers: formulas
in the studio EPUB are always images, even when an operator confirmed their LaTeX. Feeding
the confirmed regions into the edition is the next step.

**Configuration.**

```toml
[reflow]
enabled = false          # opt-in
vlm = "none"             # or "paddleocr-vl"
# vlm_python / vlm_model / vlm_device / hunspell_dict — see configs/pipeline.toml
```

Tool paths can also be overridden by `$EVILFLOWERS_VLM_PYTHON` and `$EVILFLOWERS_VLM_MODEL`.
`preflight` checks for `pandoc` and, as optional tools, `potrace` and `hunspell` when
reflow is enabled.

**Both §7 prerequisites are implemented.**

- **Running heads** are detected per book: a short line in the top band that repeats
  across the book.
- **Form or worksheet pages** become a single whole-page image. The trigger is a low
  word-like ratio together with mostly fragment paragraphs.

**Math validation changed.** It is now one batched pandoc → MathML run per book, instead
of an xelatex compile per snippet, because EPUB is the shipped format.

**Still deferred:**

- the re-typeset LaTeX PDF;
- colour stamp removal before ScanTailor (the analyzer removes coloured stamps only when
  colour survives into the page);
- a trained layout source such as DocLayout-YOLO.
