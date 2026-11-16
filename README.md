# EvilFlowers Restoration Studio

[![Tests](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/actions/workflows/tests.yml/badge.svg)](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/actions/workflows/tests.yml)
[![Docker](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/actions/workflows/docker.yml/badge.svg)](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/actions/workflows/docker.yml)
[![Documentation](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/actions/workflows/docs.yml/badge.svg)](https://evilflowerscatalog.github.io/evilflowers-books-digitalizer/)

Documentation: <https://evilflowerscatalog.github.io/evilflowers-books-digitalizer/>

![Page studio: regions coloured by kind, a disputed region selected](docs/screenshots/page-studio.png)

This repository is the **portal, the orchestrator and the lab** of the EvilFlowers book
digitalizer. It turns a tree of scanned TIFF frames into one Internet Archive "texts" item
per book. Operators review in the browser only what the machine was unsure about, and a
book can then be published to the
[EvilFlowers Catalog](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog).

It does not clean, OCR, analyse or render pages itself. Every digitalization stage runs as a
task on a standalone service worker. This repository plans the work, sends it, applies the
results, and serves the review UI.

| Part | What it is |
|---|---|
| Portal | FastAPI REST + SSE + Deep Zoom tiles + MCP, and the React SPA (`frontend/`) |
| Orchestrator | one Celery worker with beat: the job ledger, admission, the light local steps |
| Lab | notebooks and experiments in `lab/` (not shipped; see [lab/README.md](lab/README.md)) |

## What it does

- **Runs a book through five stages.** clean, analyze, formulas, render and gate, each as
  jobs on a service. A job ledger (`jobs` table) tracks every job.
- **Quality gate.** OCR confidence, disputed regions, formula fallbacks and missing pages
  give a score. A book that fails goes to a review queue; the rest are approved.
- **Page studio.** Deep Zoom viewer, region editing, one-click decisions on disputed
  regions, per-page overrides, LaTeX editing, and an interactive re-run of one page.
- **Scan marks across the book (#14).** A stamp that repeats on many leaves is one group
  with one verdict, not one question per page.
- **Title-page prefill (#15).** Title, authors, publisher and year are suggested from the
  title page. Nothing is imported until an operator confirms.
- **IA compatible.** `_meta.xml`, `_scandata.xml`, `_page_numbers.json`, `_files.xml`,
  `_jp2.zip`, `_hocr.html`, `_djvu.txt`: the output tree is an IA item.
- **Optional catalog publishing**, idempotent: a republish updates the same entry.
- **Labelling.** Hand labels for decision-model calibration, and training export of
  confirmed regions as COCO / DocLayNet JSON.

| | |
|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Book](docs/screenshots/book.png) |
| ![Swipe compare](docs/screenshots/compare-swipe.png) | ![Review queue](docs/screenshots/review-queue.png) |

## Architecture

```mermaid
flowchart LR
    SPA["SPA (React)"] -->|"REST /api/v1, SSE, tiles, /mcp"| Portal["portal<br/>(FastAPI)"]
    Portal --> DB[("Postgres<br/>books, pages, regions,<br/>runs, jobs, mark groups")]
    Portal -->|"digitalizer.run"| Broker[("Redis / Valkey<br/>Celery broker")]
    Broker --> Orch["orchestrator<br/>(Celery + beat)<br/>queue digitalizer.portal"]
    Orch --> DB
    Orch -->|"send_task + link callbacks"| Broker
    Broker --> Svc["service workers<br/>scan, ocr, analyzer,<br/>equation, render"]
    Svc <-->|"files + outcome file"| FS[("shared storage<br/>scans, artifacts")]
    Orch <--> FS
    Orch -.->|"HTTP, fail soft"| Dec["decider, decider-vision"]
    Orch -->|"optional"| Cat["EvilFlowers Catalog"]
```

The default routes are in `evilflowers_books_digitalizer/studio/config.py`
(`DEFAULT_ROUTES`, `DEFAULT_PARTS`):

| Stage | Task | Runs on |
|---|---|---|
| clean | `evilflowers_scan.clean` | scan-service |
| analyze | `evilflowers_ocr_worker.standalone.hocr` and `evilflowers_analyzer.standalone.layout` in parallel; `evilflowers_scan.erase` for confirmed "remove" boxes | ocr-service, analyzer-service, scan-service |
| formulas | `evilflowers_equation.standalone.formulas` | equation-service |
| render | `render/prepare` (local), then `evilflowers_render.book` | orchestrator, render-service |
| gate, publish | `digitalizer.local.gate`, `digitalizer.local.publish` | orchestrator |

The orchestrator's own steps are leaf planning (OpenCV spread detection), `clean/marks`,
`analyze/title_page`, `render/prepare`, the gate and publication. It consumes one queue,
`digitalizer.portal`. The portal image has no ScanTailor, Tesseract, jbig2enc, recode_pdf,
pandoc, DocRes or ONNX runtime.

Diagrams (PlantUML, with rendered SVG next to each source) are in
[docs/diagrams/](docs/diagrams/): the
[component view](docs/diagrams/component-digitalizer-target.svg),
[full run](docs/diagrams/sequence-digitalizer-full-run.svg),
[page re-run](docs/diagrams/sequence-digitalizer-page-rerun.svg),
[scan marks](docs/diagrams/sequence-digitalizer-scan-marks.svg),
[title page](docs/diagrams/sequence-digitalizer-title-page.svg),
[book states](docs/diagrams/state-digitalizer-book.svg) and
[run and job states](docs/diagrams/state-digitalizer-run-job.svg).
The design is in [docs/design/target-architecture.md](docs/design/target-architecture.md);
the API, data model and IA rules are in [docs/design/studio-spec.md](docs/design/studio-spec.md).

## Quick start (dev stack)

You need Python 3.12+, Docker (Postgres and Redis), and Node with pnpm for the SPA.

```bash
poetry install                                            # or: uv sync
docker compose -f deploy/dev/compose.yml up -d            # Postgres :55432, Redis :56379
export STUDIO_CONFIG=$PWD/configs/studio.dev.toml         # project stu-local
export STUDIO_DATABASE_URL=postgresql+psycopg://studio:studio@127.0.0.1:55432/studio
export STUDIO_REDIS_URL=redis://127.0.0.1:56379/0
mkdir -p .cache/studio                                    # beat's schedule file
pnpm --dir frontend install && pnpm --dir frontend build  # SPA -> frontend/dist

evilflowers-digitalizer studio migrate                    # schema
evilflowers-digitalizer studio serve --port 8000          # portal: http://127.0.0.1:8000
evilflowers-digitalizer studio worker -- -B               # orchestrator, queue digitalizer.portal, with beat
```

Scans and artifacts must live under the **storage root** (`storage_root` in
`configs/studio.dev.toml`, here `.data`). A run plans its jobs and then waits for the
service workers. Start the ones you need from their own repositories on the same Redis, with
`STORAGE_PATH` set to the storage root. Without them, a run stays `running` with its jobs
`dispatched`; the portal's other features (books, page studio on cleaned pages, labelling,
scheduler) still work. Details: [deploy/dev/README.md](deploy/dev/README.md).

Then **Import** on the dashboard, open a book and click **Full run**. The operator workflow
is in [docs/studio.md](docs/studio.md).

## Configuration

One TOML file, read by every command: `$STUDIO_CONFIG`, or `--config`, or
`configs/studio.toml`. Environment variables override single values.

| Setting | Where |
|---|---|
| database, broker, storage root, artifacts, scratch | `[studio]`; `STUDIO_DATABASE_URL`, `STUDIO_REDIS_URL`, `STUDIO_STORAGE_ROOT` |
| a collection (scans, naming, language, publishing, metadata) | `[projects.<slug>]` |
| stage routes, batch size, deadlines, re-send budget | `[orchestration]` |
| admission at peak hours, backpressure | `[scheduling]` |
| decision services | `[decider]`; `DECIDER_URL`, `DECIDER_VISION_URL`, `DECIDER_MODEL`, `DECIDER_TIMEOUT`, `DECIDER_ENABLED` |
| scan-mark grouping | `[marks]` |
| title-page prefill | `[title_page]` |
| hand labelling | `[labelling]` |

The storage root is required: service jobs carry paths relative to it. Configs from the batch
era (`configs/pipeline*.toml`) are gone. The project keys `layout_detectors` and
`formula_recognizer` and the `[pipeline]` keys of the retired in-process stages are rejected
at load time. A stage with `backend = "local"` is rejected too. Reference:
the docstring of `evilflowers_books_digitalizer/studio/config.py`, and
[docs/deployment.md](docs/deployment.md).

## CLI

`evilflowers-digitalizer` (also `python -m evilflowers_books_digitalizer`):

| Command | What |
|---|---|
| `studio serve [--host] [--port]` | the portal: REST, SSE, tiles, SPA, `/mcp` |
| `studio worker [-Q queues] [-- celery args]` | the orchestrator's Celery worker (default queue `digitalizer.portal`; add `-- -B` for beat, on exactly one worker) |
| `studio import <project> [collection]` | enqueue discovery of a scan tree |
| `studio run <book> [--kind full\|rerun\|render\|publish] [--pages] [--interactive]` | create and enqueue a run |
| `studio export-ia <book> [--out]` | write a book's IA item zip |
| `studio export-training <project>` | enqueue the labelled-regions export |
| `studio sample-labels`, `label`, `labelling-status`, `export-labels` | hand labelling |
| `studio mcp` | labelling tools over MCP on stdio |
| `studio migrate` (alias `db-upgrade`), `db upgrade`, `db current`, `db revision -m` | schema migrations |

## Tests

```bash
poetry run pytest                  # unit tests; no network, Docker or real workers
poetry run ruff check evilflowers_books_digitalizer tests
poetry run mypy evilflowers_books_digitalizer
pnpm --dir frontend test           # SPA unit tests
```

Tests drive the whole orchestration against stand-in services registered under the services'
real task names (`tests/studio/batch/batch_harness.py`, `studio/testing/standalone.py`). See
[docs/testing.md](docs/testing.md).

## Deployment

The image (`Dockerfile`) is `python:3.12-slim` with the SPA and the package; the command
picks the role. The k8s manifests (kustomize, namespace `digitalizer`) have two Deployments,
`portal` and `orchestrator`. The service workers are deployed from the knowledge repository
(knowledge#66). See [deploy/README.md](deploy/README.md),
[deploy/k8s/README.md](deploy/k8s/README.md) and [docs/deployment.md](docs/deployment.md).

## Retired batch pipeline

The unattended batch pipeline and its CLI (`run-book`, `run-corpus`, `monitor`, covers,
catalog manifests, the reflow EPUB code) were removed in
evilflowers-books-digitalizer#6. The last commit that has it is `c94a858`. The pages that
describe it are kept in [docs/archive/](docs/archive/).

## Layout

```
evilflowers_books_digitalizer/
├── cli.py            studio … and db … commands
├── config.py         PROJECT_ROOT
├── logs.py           text / JSON logging
├── studio/           domain, ports, events, config
│   ├── application/  use cases (one transaction each)
│   ├── api/          FastAPI: REST, SSE, tiles, MCP, SPA
│   ├── workers/      Celery app, tasks, stages (orchestrator side)
│   ├── orchestration/  job ledger, contract, scheduling, appliers
│   ├── adapters/     scans, artifacts (IA tree), imaging (spreads, tiles), metadata,
│   │                 catalog, ia, decider
│   └── testing/      stand-in services and engine doubles
├── metadata/         Excel catalog, OPAC / Open Library lookup, OCR colophon extraction
├── catalog/          book metadata -> catalog entry mapping
└── db/               SQLAlchemy models, Alembic migrations
configs/studio.*.toml studio configs: dev and k8s
frontend/             the SPA (React + TypeScript + Vite)
deploy/               dev stack and k8s manifests
docs/                 documentation (Zensical site)
lab/                  notebooks and experiments
```
