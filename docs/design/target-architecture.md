# Target architecture: portal + orchestration + lab, digitalization on the shared workers

> **Status (2026-11-17): implemented in the repository, not yet deployed or run on a real book.**
> This page was written as a proposal (2026-11-16); the code now follows it, with the deviations
> below. What remains is deploying the service workers and a parity run on a real book
> (evilflowers-books-digitalizer#4, knowledge#66). The sections below are the design as proposed;
> where they differ from the code, this note and [architecture.md](../architecture.md) win.
>
> **Implemented**
>
> - The repository is the portal, the orchestrator and `lab/`. The batch pipeline, its CLI, `pipeline/`,
>   `reflow/`, `vlm_service/`, `sources/`, `covers/`, `catalog/publisher.py` and the in-process adapters
>   (ocr, layout, formula, render, ScanTailor) are deleted (#6). The last commit that has them is `c94a858`.
> - Every stage runs as a service task (§2, §3). The routes are `DEFAULT_ROUTES` and `DEFAULT_PARTS` in
>   `studio/config.py`. `backend = "local"` for clean, analyze, formulas and render is rejected, and the
>   in-process fallback of §8 step 3 is gone.
> - The job ledger, `link` / `link_error` callbacks, `digitalizer.reconcile`, token-checked outcome files,
>   bulk and interactive lanes, admission (peak hours, backpressure, pause), publication as an option (§2).
> - One Celery queue, `digitalizer.portal`. The image has only the SPA and the package; `deploy/k8s` has two
>   Deployments, `portal` and `orchestrator`.
> - O3 (job size): 32 leaves per bulk job, one per interactive job, one job per book for render.
> - `lab/` holds the notebooks, pinned to `c94a858` (`lab/README.md`).
>
> **Differs from the proposal**
>
> - §3, the orchestrator column: `covers/` is gone (no covers are generated); `metadata/` stays (Excel, OPAC,
>   OCR colophon lookup, called at `render/prepare`).
> - §3, the `render` row's "task `evilflowers_render.book`" has a local `render/prepare` step before it, and
>   the orchestrator, not the service, writes the IA XML and the text files.
> - §3, `analyze` also uses `evilflowers_scan.erase` (confirmed "remove" boxes) and has the local tail step
>   `title_page`; `clean` has the local tail step `marks` (`TAIL_PARTS`).
> - §3, the figures task of image-service (`evilflowers_image.standalone.figures`) and the tables task are
>   not routed. There is no figures stage.
> - §6, model-assisted checks: not inside the extractors. The orchestrator calls the decision services
>   itself, over HTTP, fail soft (`[decider]`, `studio/adapters/decider.py`): scan marks grouped across the
>   book (#14, decider-vision) and the title-page prefill (#15, decider). The shared `judges` library of
>   evilflowers-worker is not a dependency of this repository. The metadata draft from the title page uses
>   the text decider, not the Ollama vision model of §5.
> - §6, thresholds: `[marks] hide_above` / `keep_above` are unset until knowledge#65 calibrates
>   `scan_mark`, so every decider verdict stays `review`.
> - O7 (old batch CLI): removed in #6 instead of after a parity run on 10 books. The parity run on a real
>   book is still open (#4).
>
> **Superseded**
>
> - The deployment and component parts of the single-image studio. The PlantUML sources of the old studio
>   (`docs/design/*-restoration-studio.puml`) are deleted; the diagrams below replace them.
> - `studio-spec.md` stays valid for the API, the SPA and the Internet Archive data model; see its own
>   status note.

Diagrams (`docs/diagrams/`, ELVIRA theme, rendered `.svg` + `.png` next to each `.puml`):

| Diagram | Answers |
|---|---|
| [component-digitalizer-target](../diagrams/component-digitalizer-target.svg) | which parts exist, which repository owns what, which shared services are reused |
| [deployment-digitalizer-dev](../diagrams/deployment-digitalizer-dev.svg) | where it runs on dev k3s (control, gateway, app01) |
| [sequence-digitalizer-full-run](../diagrams/sequence-digitalizer-full-run.svg) | a book from run to (optional) publication, through the job ledger |
| [sequence-digitalizer-page-rerun](../diagrams/sequence-digitalizer-page-rerun.svg) | an operator's page re-run, and how a superseded run is refused |
| [state-digitalizer-book](../diagrams/state-digitalizer-book.svg) | book lifecycle |
| [state-digitalizer-run-job](../diagrams/state-digitalizer-run-job.svg) | run and job (ledger) state machines |
| [sequence-digitalizer-scan-marks](../diagrams/sequence-digitalizer-scan-marks.svg) | scan marks grouped across the book, one decision per group (#14) |
| [sequence-digitalizer-title-page](../diagrams/sequence-digitalizer-title-page.svg) | title-page prefill and its confirmation (#15) |

## 1. Decisions taken (2026-11-16)

| Topic | Decision |
|---|---|
| Formulas | **PaddleOCR-VL**, called from equation-service through a new backend |
| Broker | **Valkey** `valkey.shared` (Redis protocol), **db 8** (#8; the value lives in the knowledge repository's `keyvalue.yml` and may change) |
| Namespace | **`digitalizer`** |
| Existing extractors | They report to the catalog, so the digitalizer **does not use their catalog tasks**. Each existing repository gets a **second, catalog-free ("standalone") task** that shares the same core code. The catalog-based pipeline runs on the PDF only after publication. |
| Publication | **Optional.** A book can be finished, approved and exported (PDF, PDF/A, EPUB, IA item) without ever reaching the catalog |
| OCR | the existing **evilflowers-ocr-service**, with a new standalone task |
| Model-assisted checks | inside the extractors, through a shared library (§6); no separate service |

## 2. The idea

1. **This repository keeps the portal, the orchestration and the lab.**
   - Portal: the SPA and the API.
   - Orchestration: the domain, the database and a job ledger.
   - Lab: notebooks, experiments, training export.

   It stops running ScanTailor, Tesseract, layout models or PDF tooling itself.
2. **Every heavy stage runs in a worker built on [evilflowers-worker](https://github.com/EvilFlowersCatalog/evilflowers-worker).**
   - Where a repository already does the job, it gets a standalone task.
   - Where none does, a new repository is created (§4).
3. **The standalone task contract** (one model in the kit, shared by every service):
   - kwargs: `run_id`, `job`, `token`, `inputs` (leaf image paths or a PDF, relative to `STORAGE_PATH`),
     `output_dir` (job-scoped: `<item>/_studio/jobs/<job>/`), `outcome` and the job's own options;
   - the worker writes its output, then the **outcome file** last (`evilflowers_worker.outcome.write_outcome`);
   - the task returns a compact summary;
   - the worker holds no database or catalog credential.
4. **Celery tells the orchestrator when a job ends.** The orchestrator sends each command with
   `send_task(name, kwargs=…, queue=…, link=…, link_error=…)`:
   - `link` is the success callback: `digitalizer.job_done`, with the `job_id` bound, on queue `digitalizer.portal`.
     Celery applies it with the task's return value.
   - `link_error` is the failure callback: `digitalizer.job_failed` in its single-argument form, which receives the
     failed task id. That is the form the Celery docs prescribe for tasks sent by name.

   This needs **no result backend** (only chords do) and **no separate event listener**: the callback is an ordinary
   message on the orchestrator's own queue. The orchestrator then reads the outcome file, checks the token, promotes
   the job-scoped output into the item and sends the next jobs.
5. **One beat task, `digitalizer.reconcile` (every minute), covers what the callbacks cannot:**
   - a worker died and the message was redelivered until the delivery cap;
   - a callback was lost;
   - a job ran past its deadline.

   It first applies an outcome file that exists without a callback. Only when there is none does it re-send the job
   with a new token; once the job's budget is spent, the run fails.
6. **The stage order stays as it is today** (`orchestrate.STAGES`):
   - FULL = clean → analyze (hOCR ∥ layout) → formulas → render → gate;
   - RERUN = clean → analyze → formulas;
   - RENDER, PUBLISH and EXPORT are runs of their own.

   A passing gate approves the book. PUBLISH runs only when the project has a catalog target (optional).
7. **Bulk and interactive work share one contract.** Only the queue differs:
   - `<service>.standalone` is scaled by KEDA from 0 to N;
   - `<service>.standalone.interactive` has one replica that is always on, in its own Deployment.

   The kit consumes one message at a time per process (prefetch 1), so a single worker serving both queues would make
   an operator wait behind a bulk batch. Redis priorities (`queue_order_strategy: priority`, 0 = highest) are only
   approximate, and they cannot interrupt a batch that is already running. They may still order work *within* a
   queue, but they don't replace the interactive lane.
8. **The broker timeout is one value per Redis db.** The Celery docs: when several applications share a broker, the
   shortest `visibility_timeout` wins. Every consumer of db 8 sets the same value; the kit's default is 12 h.

## 3. Which repository does which stage

| Stage | Repository | New task (standalone) | Shared core (already in the repository) | Change to the core |
|---|---|---|---|---|
| discover, plan leaves, gate, metadata, IA documents, publish, export | **this repo** | orchestrator tasks `digitalizer.*` | `studio/application`, `adapters/catalog`, `adapters/ia`, `metadata/`, `covers/` | — |
| clean pages (split, deskew, crop, despeckle, DocRes), previews, DZI | **evilflowers-scan-service** (new) | `evilflowers_scan.clean` | moved from this repo: `pipeline/steps/{scantailor,docres}.py`, `adapters/imaging/*` | — |
| OCR → hOCR per leaf | **evilflowers-ocr-service** | `evilflowers_ocr_worker.standalone.hocr` | `engines.py` (Tesseract 5.5 `slk`, RapidOCR) | an engine method that OCRs one **image** to hOCR; today the core is ocrmypdf on a PDF (`processing.py`) |
| layout regions (figure, table, formula) | **evilflowers-analyzer-service** | `evilflowers_analyzer.standalone.layout` | `layout.py` (Docling heron, batched, `{page, seq, bbox, caption}`) | a converter for **image** input next to the PDF one; regions written to the outcome, not sent onward |
| formulas → LaTeX | **evilflowers-equation-service** | `evilflowers_equation.standalone.formulas` | `latex.py` (`Recognizer` protocol, `clean`, sympy `normalize`), `crops.py` | a new **`paddleocr-vl` backend** (HTTP to vlm), beside `onnx` and `ollama` |
| figures: crop, filter, "real figure?", category | **evilflowers-image-service** | `evilflowers_image.standalone.figures` | `crops.py`, `figures.py` (size, aspect and coverage filters), `captions.py` (Ollama) | crops from leaf images |
| tables | **evilflowers-text-service** | later: `…standalone.tables` | `TableExtractor` (pdfplumber needs a text layer) | after render, or not at all for the digitalizer |
| MRC PDF, PDF/A-2b (JP2), EPUB, JP2 zip, `_djvu.txt` | **evilflowers-render-service** (new) | `evilflowers_render.book` | moved from this repo: `pipeline/steps/{render,pdfa,finalize,reflow}.py`, `reflow/edition.py`, `adapters/ia/images.py` | — |
| formula model server | **evilflowers-vlm-service**: PaddleOCR-VL, HTTP `POST /v1/formulas`, in namespace `shared` | — | moved from this repo: `vlm_service/` | CI-built image (today it is pushed by hand) |
| paragraphs, embeddings, outline | catalog-side pipeline **after publication** | — | analyzer → text, image, equation (unchanged) | — |

Code that leaves this repository: `pipeline/` and the batch CLI (`runner.py`, `batch.py`, `monitor.py`, `dashboard.py`,
`progress.py`, `reporting.py`, `db/store.Recorder`), `vlm_service/`, and the in-process stages
`studio/workers/stages/{clean,analyze,formulas,render}.py` with their adapters. `notebooks/` and `experiments/` move
under `lab/`.

Leaves are images, while the existing cores read PDFs (PyMuPDF crops by page fractions, Docling's PDF pipeline). So
each standalone task needs a small **page source** that yields a page as an image from either kind of input. That is
the main code change in the analyzer, equation and image repositories. The catalog tasks keep their PDF path
unchanged.

## 4. Repositories

**Create (2):**

| Repository | Why it cannot live in an existing one |
|---|---|
| `evilflowers-scan-service` | Page restoration (ScanTailor, OpenCV, DocRes) has no home: the catalog never sees raw scans |
| `evilflowers-render-service` | Book assembly (recode_pdf MRC, jbig2enc, JP2/PDF/A-2b, EPUB) has no home: the catalog only receives finished PDFs |

**Created as well:** `evilflowers-vlm-service`, the PaddleOCR-VL HTTP server (`POST /v1/formulas`), moved from
`vlm_service/`. It has a CI-built image with the weights baked in, and is deployed in namespace `shared` by
knowledge#67.

**Extend (standalone tasks, shared core):** evilflowers-ocr-service, evilflowers-analyzer-service,
evilflowers-equation-service, evilflowers-image-service (and later evilflowers-text-service).

**Extend (contract and wiring):**
- **evilflowers-worker:** `contract.StandaloneJob` (the kwargs in §2.3), helpers for the `link`/`link_error`
  callbacks, the judge library (§6) as an HTTP-only extra, and docs that the system of record can be any
  orchestrator.
- **knowledge:**
  - namespace `digitalizer`, Valkey db 8 and its exporter, KEDA objects per standalone queue;
  - NFS with `digitized` read-write for control;
  - Ollama's NetworkPolicy admitting `digitalizer`;
  - vlm in `shared`;
  - a second Deployment per extended service (same image, broker db 8, standalone queues).

## 5. Shared services reused on dev

| Service | Used for | Fact source |
|---|---|---|
| PostgreSQL 18 via PgBouncer `:6432` (k8s-bridge) | database `digitalizer` | knowledge entity k8s-bridge |
| Valkey `valkey.shared:6379`, db 8 | commands and link callbacks (broker, #8) | knowledge `keyvalue.yml` (vault Infrastructure/Key-Value) |
| Redis `:6379` (k8s-bridge), db 7 | the catalog's queues, read for backpressure only | knowledge Extraction-Pipeline |
| NFS on gateway (`roles/nfs_server`, written, not applied) | `raw-scans` read-only and `digitized` read-write PVs, both under `/data/library` (the kit's `STORAGE_PATH`) | knowledge NFS-Storage |
| Ollama `elvira-assistant` (vision) on app01 | metadata draft from title pages, figure captions, judges | knowledge Shared-Inference |
| tei-rerank on app01 | association scores (caption ↔ figure) | knowledge Shared-Inference |
| Laya (announced) on app01 | verdicts of the judges | — |
| dev catalog `/api/v1` | optional publication | knowledge Digitalizer |
| LGTM (alloy-node OTLP/HTTP `:4318`) | the kit's observability contract | knowledge Deploy-a-Service |

## 6. Model-assisted verification (in the extractors)

**Goal:** better extractor quality. Extractors **propose** (regions, OCR lines, captions, formulas). A **judge**
answers one narrow question about an uncertain proposal and either accepts it, rejects it, or marks it `disputed`
for the review screen. A judge never edits silently.

**Models at hand**

| Model | Where | Good for | Cost (measured) |
|---|---|---|---|
| `decider`: Laya 0.4.34, **text only** (images → 422) | app01 `shared`, `POST /v1/systemone` (knowledge#64: written, not applied) | text verdicts: OCR line plausible? caption ↔ reference? | calibration and latency: knowledge#65 |
| `decider-vision`: Strands Decider 2B (Qwen3.5) | app01 `shared`, `POST /v1/systemone` with `images` (knowledge#64) | visual verdicts: figure / stamp / decoration / blank, with a probability | knowledge#65 |
| Ollama `elvira-assistant` (Qwen3.6-35B-A3B, vision, tools) | app01, live | looking at a crop plus its context; structured JSON answers | ~25 s per 1,000 new prompt tokens cold, ~9 tok/s output, 3 parallel slots (Shared-Inference, 2026-11-09) |
| `tei-rerank` (bge-reranker-v2-m3) | app01, live | pair scores: caption ↔ figure description, paragraph ↔ "see Fig. 3.2" | cheap, batched |
| PaddleOCR-VL | vlm, proposed | a second reading of a formula or a line | per crop |

**The three questions, and where each is asked**

1. **"Is this a real feature?"** For example, is a region a figure, or a stamp, a library sticker, bleed-through, a
   scan edge or a rule?
   - *Signals first:* area, aspect ratio, ink density, position (margin or gutter), and repetition across pages.
   - *Judge:* only the uncertain band goes to Laya or the vision model, with the crop and the page thumbnail.
   - *Where:* scan-service for artefacts, and image-service's `figures.py` filters for figures.
2. **"Is this an error?"**
   - *OCR line* (ocr-service): low confidence **and** words outside a Slovak lexicon. A second engine or the VLM
     reads the line crop, and a correction is accepted only when both readings agree; otherwise the line is
     `disputed`.
   - *Formula* (equation-service): `clean` + sympy `normalize` already exist. Add rendering the LaTeX and comparing
     it with the crop; on disagreement the region is `disputed`.
   - *Page sequence* (orchestrator, deterministic): printed page numbers must increase, and duplicate or blank
     leaves are flagged.
3. **"Is this really associated with this?"**
   - *Caption ↔ figure, table or formula* (analyzer and image): candidates come from Docling's `caption_text` and
     geometry, then `tei-rerank` scores them; Laya decides when the top two are close.
   - *Metadata ↔ book* (orchestrator): a vision draft from the title page is cross-checked against the OPAC by ISBN.

**Rules**
- **Cheap signals settle most cases.** Models see only the uncertain band, within a per-job token budget.
- **Structured output only:** `{verdict, confidence, reason, model@revision}`, stored as provenance on the region or
  line.
- **The review screen is the labelling tool.** Operator decisions on `disputed` items measure each judge in the lab,
  and a judge is switched on only once it beats the signals-only baseline.
- **Catalog side already started:** image-service#6 calls `decider-vision` for keep/review/hide bands. The library
  generalises that call, so the standalone tasks use exactly the same one.
- **Shared code lives in a library:** question schemas, prompts, and the HTTP clients for `decider`,
  `decider-vision`, Ollama and tei-rerank.
  It is an HTTP-only `judges` extra of evilflowers-worker, used in-process by every extractor, on both the catalog and
  the standalone side.

## 7. Still open

| # | Question | Recommendation |
|---|---|---|
| O1 | Where the library is mounted | NFS (apply NFS-Storage, with `digitized` read-write for control). The fallback, hostPath on gateway, pins workers to its 8 cores, which prod also uses |
| O2 | Where workers run | control first; app01 E-cores later if its `inference` taint admits a `batch` balloon |
| O3 | Job size | batches of 32 leaves for bulk work, one leaf for interactive work, one job per book for render |
| O5 | decider and decider-vision latency and thresholds | knowledge#65 (calibration) |
| O6 | EPUB (reflow edition) | in render-service, behind a project flag; still lab quality for Slovak prose |
| O7 | Old batch CLI | retire after parity on 10 books |

## 8. Next steps

1. Create `evilflowers-scan-service` and `evilflowers-render-service` from the kit's reference layout.
2. File issues:
   - **evilflowers-worker:** `StandaloneJob`, the link-callback helpers, the `judges` extra;
   - **ocr, analyzer, equation, image services:** page source + standalone task (equation: also the `paddleocr-vl`
     backend);
   - **this repo:** job ledger (migration), `digitalizer.job_done` / `job_failed` / `reconcile`, a dispatcher that
     replaces the in-process stages, a slim portal image, optional publication, `lab/`, k8s manifests;
   - **knowledge:** §4 wiring.
3. Port one stage end to end first (clean in scan-service), keeping the other stages in-process behind the same ledger
   until their standalone tasks exist.
