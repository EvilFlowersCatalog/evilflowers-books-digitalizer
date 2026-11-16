# Testing & local run

How to check the code, and how to run the whole studio on your machine. The batch go-live
runbook (`run-book`, `run-source`, `monitor`, `validate-catalog`) is retired; see
[Archive](archive/digitalization_pipeline_report.md).

## 0. Prerequisites

| Need | For |
|---|---|
| Python 3.12 or 3.13, Poetry (or uv) | the package and the tests |
| Docker | Postgres and Redis for the local run |
| Node and pnpm | the SPA (`frontend/`) |

No system tools are needed: no Tesseract, ScanTailor, jbig2enc or pandoc. Those live in the
service images, not here.

```bash
poetry install                       # main + dev groups
```

## 1. Fast checks

These match the CI jobs in `.github/workflows/tests.yml`:

```bash
poetry run pytest                                       # unit tests
poetry run ruff check evilflowers_books_digitalizer tests
poetry run ruff format --check evilflowers_books_digitalizer tests
poetry run mypy evilflowers_books_digitalizer
pnpm --dir frontend install && pnpm --dir frontend test # SPA unit tests (vitest)
pnpm --dir frontend build                               # type-check + bundle
```

The unit tests need no network, no Docker and no running workers: SQLite in memory, the
in-memory event bus and Celery in eager mode (`STUDIO_EAGER=1`). `pyproject.toml` defines an
`integration` marker (opt-in with `pytest -m integration`, against the dev stack); no test
uses it at the moment.

CI runs the tests on Python 3.12 and 3.13 with coverage, and builds the SPA stage of the
Dockerfile.

## 2. What the tests cover

| Area | Where |
|---|---|
| Orchestration end to end: runs, the job ledger, admission, reconcile | `tests/studio/batch/test_ledger.py`, `test_workers.py`, `test_worker_guards.py` |
| Each stage against its stand-in service | `test_clean_service.py`, `test_analyze_service.py`, `test_formulas_service.py`, `test_render_service.py` |
| The service contract (kwargs, outcome file, token) | `test_standalone_contract.py` |
| Scan-mark groups (#14) and title-page prefill (#15) | `test_marks.py`, `test_title_page.py` |
| API, use cases, labelling, scheduler, tiles | `tests/studio/interactive/` |
| IA documents, export and import, the catalog publisher | `tests/studio/publish/` (one test reads our `scandata.xml` with archive-pdf-tools) |
| Config loading, storage root, retired keys | `tests/studio/test_config_storage.py`, `test_contracts.py` |
| CLI: the retired batch commands are gone, `title_page` cannot be labelled by hand | `tests/test_cli.py` |
| Metadata providers, ISBN lookup, logging, the DB | `tests/test_metadata.py`, `test_isbn_lookup.py`, `test_logs.py`, `test_db.py` |

### The service stand-ins

The orchestrator only sends jobs and applies outcomes, so the tests register a stand-in for
each service under the service's real task name:

| Task name | Stand-in |
|---|---|
| `evilflowers_scan.clean`, `evilflowers_scan.erase` | `scan_clean`, `scan_erase` |
| `evilflowers_ocr_worker.standalone.hocr` | `ocr_hocr` |
| `evilflowers_analyzer.standalone.layout` | `analyzer_layout` |
| `evilflowers_equation.standalone.formulas` | `equation_formulas` |
| `evilflowers_render.book` | `render_book` |

The stand-ins are in `evilflowers_books_digitalizer/studio/testing/standalone.py`. They follow
the services' contract (the same kwargs, an outcome file written last) and drive engine
doubles from `studio/testing/engines.py` and `fakes.py`. `tests/studio/batch/batch_harness.py`
builds a `Studio` with an eager Celery app, SQLite, the in-memory bus and these stand-ins.
Use it for a new stage test. The module is also the reference of what a service task must do.

## 3. Run the whole studio locally

Follow [deploy/dev/README.md](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/blob/master/deploy/dev/README.md):

```bash
docker compose -f deploy/dev/compose.yml up -d
export STUDIO_CONFIG=$PWD/configs/studio.dev.toml
export STUDIO_DATABASE_URL=postgresql+psycopg://studio:studio@127.0.0.1:55432/studio
export STUDIO_REDIS_URL=redis://127.0.0.1:56379/0
mkdir -p .cache/studio
evilflowers-digitalizer studio migrate
evilflowers-digitalizer studio serve --port 8000
evilflowers-digitalizer studio worker -- -B          # the orchestrator, with beat
deploy/dev/smoke.sh                                  # GET /api/v1/projects on :8000
```

`configs/studio.dev.toml` has one project, `stu-local`, with scans under `.data/raw-scans` (the
storage root is `.data`). Put a few books there as
`.data/raw-scans/<collection>/<book>/*.tif`. Never point the dev config at the real scan
volume.

What works without the services: importing books, the book and page screens on already
cleaned pages, regions, review, labelling, the scheduler, the REST API and MCP. A full run
needs the service workers.

To run a book end to end, start the service workers you need from their repositories
(evilflowers-scan-service for clean; ocr, analyzer, equation and render for the other stages),
on the same Redis, with `STORAGE_PATH` set to the absolute path of `.data`. Then **Import**,
open a book, **Full run**. If a stage's service is missing, its jobs stay `dispatched` and the
run stays `running`.

The decision services exist only in the cluster. Without `[decider]` URLs, scan marks stay
`review` and no title-page metadata is suggested; to try them, uncomment `[decider]` in
`configs/studio.dev.toml` and point it at a reachable decider.

## 4. Open work

A parity run on a real book with the deployed services is the remaining part of
evilflowers-books-digitalizer#4 (knowledge#66).

## Quick reference

| Goal | Command |
|---|---|
| Unit tests | `poetry run pytest` |
| One test file | `poetry run pytest tests/studio/batch/test_marks.py` |
| Lint, types | `poetry run ruff check …`, `poetry run mypy evilflowers_books_digitalizer` |
| SPA tests | `pnpm --dir frontend test` |
| SPA without a backend | `pnpm --dir frontend dev:mock` |
| Schema | `evilflowers-digitalizer studio migrate`, `db current` |
| Dev stack up, down | `docker compose -f deploy/dev/compose.yml up -d`, `… down` |
