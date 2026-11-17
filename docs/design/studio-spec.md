# Restoration Studio — implementation contract

> **Status (2026-11-17): partly superseded.** The data model, the IA compatibility rules, the REST
> API and the SPA still apply. The execution model changed with the move to service workers
> (evilflowers-books-digitalizer#6), described in [target-architecture.md](target-architecture.md)
> and [architecture.md](../architecture.md).
>
> **Still valid:** *Internet Archive compatibility*, *REST API* (updated below), *SPA*, the
> DTO shapes.
>
> **Superseded:**
>
> * *Rules for every implementer* and *Package layout and ownership*: the build-time rules of the
>   first implementation. They name modules that are deleted (`reflow/`, `pipeline/steps/*`,
>   `catalog/publisher.py`, `imaging.py`, the `studio/adapters/{ocr,layout,formula,render}` adapters,
>   DocLayout-YOLO, the VLM sidecar, `db.Recorder`).
> * *Queues and tasks (Celery)*: the queues `interactive`, `cpu`, `ai`, `io`, the `studio.*` tasks,
>   `studio.recover_runs` and the liveness columns. A job ledger replaces the Celery canvas,
>   `heartbeat_at` / `dispatched_at` and `recover_runs`; there is one queue, `digitalizer.portal`; every
>   stage runs on a service. The current tasks are in the replacement table in that section.
> * *Detectors and formulas* (adapter names `heuristic`, `doclayout`, `paddleocr-vl`,
>   `paddleocr-vl-http`, `layout_detectors`, `formula_recognizer`): gone; the services choose their
>   engines and the config keys are rejected.
> * *Testing the whole system locally*: use [deploy/dev/README.md](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/blob/master/deploy/dev/README.md).
> * Publication is optional (`[publish] target = "none"` ends a book in `approved`).
>
> **Added since:** scan-mark groups (#14), the title-page prefill (#15), the scheduler endpoints,
> and the labelling tasks `scan_mark` and `title_page` (#7); see the REST table and
> [studio.md](../studio.md).

The contract for the data model, the API and the IA rules. Diagrams: `docs/diagrams/`
(the old `docs/design/*.puml` files are deleted).

**Rules for every implementer** (historical: the build-time rules of the first implementation)

- **Own only your directory** (table below). If you need a change in a shared contract file
  (`studio/domain.py`, `studio/ports.py`, `studio/events.py`, `studio/metadata.py`,
  `studio/config.py`, `db/models.py`, migrations), do not edit it. Write the request into
  your final report, and the integrator applies it.
- **Dependency rule.** `studio/domain.py` imports nothing from the project.
  `studio/application/` imports only `domain`, `ports`, `events`, `metadata` and `config`,
  plus the SQLAlchemy session and models through `studio/application/uow.py`.
  Adapters implement ports. Shells (`api`, `workers`, `cli`) wire adapters into use cases.
- **Modern Python** (3.12+): dataclasses with slots, `StrEnum`, `type` aliases, `match`,
  full type hints.
- **ruff and mypy must be clean.** Keep functions small (SOLID, DRY, KISS). No new framework
  without a reason written in the module docstring.
- **Reuse** what exists: `reflow/` (layout heuristics, anchoring, VLM), `pipeline/steps/*`
  (ScanTailor, OCR, render, PDF/A), `catalog/publisher.py` (`evilflowers-protocol`),
  `imaging.py`. Wrap them in adapters; do not copy them.
- **Tests** live in `tests/studio/<area>/` and use pytest:
  - unit tests run without network, Docker or Celery workers (SQLite in memory,
    `task_always_eager`);
  - integration tests use real tools or the dev stack and are marked
    `@pytest.mark.integration`; they are opt-in (`pytest -m integration`; the marker and
    `addopts = -m 'not integration'` live in `pyproject.toml`). The dev stack is:
    `docker compose -f deploy/dev/compose.yml up -d`, with
    `STUDIO_DATABASE_URL=postgresql+psycopg://studio:studio@127.0.0.1:55432/studio` and
    `STUDIO_REDIS_URL=redis://127.0.0.1:56379/0`.
- **Never** read the real 10 TB volume. Local data:
  - `/private/tmp/claude-501/-Users-jdubec-Projects-EvilFlowers-evilflowers-books-digitalizer/6dab462b-be87-4faf-8b44-afb98fe19ba7/scratchpad/e2e/scans`
    (a synthetic TIFF tree `fei/E2E_FYZIKA_VEKTORY/scan_000N.tif`);
  - `output/` (legacy produced books).

## Package layout and ownership (historical)

> The adapters `ocr`, `layout`, `formula`, `render` and `imaging/scantailor` listed below are
> deleted (#6). The current layout is in [architecture.md](../architecture.md#parts).

The split is by *side*: interactive and batch both code against ports, DB models and
events. Their only touchpoint is the `runs` row (the API creates it and enqueues
`studio.run`; workers consume it).

| Path | Owner | What |
|---|---|---|
| `studio/{domain,ports,events,metadata,config}.py`, `studio/adapters/{eventbus,queue}.py`, `studio/testing/fakes.py` | contract (integrator, **frozen**) | vocabulary, rules, interfaces, shared glue, test doubles |
| `db/models.py`, `db/migrations/versions/0002_studio.py` | contract (**frozen**) | schema |
| `studio/application/` | **interactive** | use cases (ingest, review, runs, publish trigger, metadata edit), unit of work, DTOs (pydantic) |
| `studio/api/` | **interactive** | FastAPI app: REST, SSE, DZI tiles, serves the SPA from `STUDIO_STATIC_DIR` (default `frontend/dist`) |
| `cli.py`: new `studio` subcommand group (`serve`, `worker`, `import`, `run`, `export-ia`, `export-training`) | **interactive** | CLI shell |
| `studio/workers/` | **batch** | Celery app, tasks, routing, beat |
| `studio/adapters/{scans,artifacts,imaging,ocr,layout,formula,render,metadata}/`, `studio/adapters/registry.py` | **batch** | pipeline adapters; the registry builds a project's adapters by name |
| `studio/adapters/catalog/`, `studio/adapters/ia/` | **publish** | EvilFlowers publisher (upload; republish replaces files upload-then-delete; `files_base_url`/external_url refused until the catalog supports it, see `catalog-protocol-requests.md`) and Internet Archive export/import |
| `frontend/` | **spa** | React + TS + Vite SPA (builds to `frontend/dist`; never writes into `studio/`) |
| `deploy/k8s/`, `deploy/dev/`, `Dockerfile` | **deploy** | kustomize manifests, image, dev stack |
| `docs/`, `README.md`, removal of legacy `web/` (done 2026-11-07) | integrator | docs, screenshots, cleanup |

**Agent rules (in addition to the ones above)**

- Run `pytest` **only on your own** `tests/studio/<area>/`, plus `tests/studio/test_contracts.py`
  and `tests/studio/test_glue.py`. Never run the full suite: other agents' half-written tests
  will break it.
- Unit tests use `studio.testing.fakes.sqlite_session()` and the fakes.
  **Only the integrator touches the dev Postgres/Redis.** Concurrent `alembic upgrade` races.
- **Frozen files:** the contract files above, `pyproject.toml`, `poetry.lock`, `tests/conftest.py`,
  `.gitignore`, `configs/*` (except your own new `configs/studio.*.toml` example, deploy
  only). Only deploy edits `Dockerfile`. Report new dependencies in your final report; do not
  install them. The one exception is the spa agent's `pnpm install` in `frontend/`.
- **Never load model weights in tests.**
  - DocLayout-YOLO: implement the adapter behind config (`layout_detectors = ["yolo"]`),
    but never load the `.pt` file. Loading the pickle is blocked in this environment. The
    default detector stays `heuristic` (`reflow.analyze`).
  - VLM: never invoke the tool venv from tests. Use `FakeFormulaRecognizer`.
- Legacy `web/` and the `serve` command were **removed** (2026-11-07); the SPA +
  `studio/api` replace them. Its `ImageCache` lives on as
  `studio/adapters/imaging/renditions.py`. `db.Recorder` stays for the classic CLI path.
- Your final report must list the files changed, the tests and their results, any requested
  contract changes, and new dependencies.

## Internet Archive compatibility (mandatory)

The data model and the output tree mirror IA "texts" items, so a book can be exported to
or imported from archive.org. The IA tools we already use (archive-pdf-tools,
archive-hocr-tools, BookReader) also work on our files directly. Sources: the archive.org
metadata schema, archive-pdf-tools `scandata.py`, BookReader `BookModel.js`, and Scribe3
`scandata.py` (research report, 2026-11-07).

**Identifier.** `books.item_id` = `ProjectConfig.item_identifier()`: lowercase
`[a-z0-9][a-z0-9._-]{4,79}`. It is **permanent**: never renamed, and every artifact is
named after it.

**Book metadata.** `studio.metadata.ItemMetadata` follows IA `_meta.xml`:
- the field names are IA's, and repeatable fields are lists;
- `language` uses MARC codes (`slo`, not `slk`; see `marc_language()`);
- `date` is `YYYY`, `scandate` is `YYYYMMDDHHMMSS` UTC;
- custom fields are prefixed `evilflowers_`.

The DB column is `books.item_metadata`.

**Leaves.** One `pages` row per captured page image is an IA *leaf*:
- `pages.index` = `leafNum`: 0-based and contiguous.
- Columns follow scandata: `page_type` (`PageType`, IA spelling), `hand_side` (LEFT/RIGHT),
  `add_to_access` (addToAccessFormats), `page_number` (printed, a string),
  `crop_box` (`[x, y, w, h]`, after rotation), `rotate_degree`, `skew_angle`, `dpi` (ppi).
- **Exclusion is driven only by `add_to_access`.** `PageType` merely seeds its default
  (`default_add_to_access`).
- Two indices exist: the *leaf* index (images, hOCR `page_%06d`, scandata) and the *access*
  index (PDF/EPUB pages, viewer, IIIF canvases). `metadata.access_index()` maps between
  them. Every renderer uses access leaves only.

**Output tree** (ArtifactStore, `<artifacts_root>/<project>/<item_id>/`):

| kind | file | source |
|---|---|---|
| `meta_xml` | `<id>_meta.xml` | original |
| `scandata_xml` | `<id>_scandata.xml` (root `<book>` with `<bookData>` incl. `pageNumData` assertions, then `<pageData><page leafNum=…>`; archive.org's own order, parsers look elements up by name) | original |
| `images` | `<id>_jp2.zip`, holding `<id>_jp2/<id>_%04d.jp2` (cleaned pages, all leaves) | original |
| `hocr` | `<id>_hocr.html` (one `ocr_page` per leaf with `id="page_%06d"`; excluded leaves are empty pages) | derivative |
| `page_numbers` | `<id>_page_numbers.json` (format-version "2", access leaves only) | derivative |
| `pdf` | `<id>.pdf` (distribution, JPEG MRC; made by render-service, recode_pdf with `--scandata`) | derivative |
| `pdfa` | `<id>_archival.pdf` (PDF/A-2b JP2 master) | derivative |
| `epub` | `<id>.epub` (reflow edition) | derivative |
| `text` | `<id>_djvu.txt` (plain text, IA's name) | derivative |
| `files_xml` | `<id>_files.xml` (local manifest: name, source, format, md5, sha1, size) | metadata |
| (studio only) | `<id>_studio/leaves/<%04d>.webp`, `<id>_studio/dzi/<%04d>.dzi` + `_files/`, `<id>_studio/regions.json` | studio |

The IA adapter (`studio/adapters/ia/`) writes and reads `_meta.xml`, `_scandata.xml`,
`_page_numbers.json` and `_files.xml`, and builds the IA export zip. Two seams:

- `ports.IaExporter.export_item(item_id, metadata, leaves, files, leaf_images, out)` takes
  plain data, so the API path needs no database access in the adapter
  (`adapters.ia.build_ia_exporter(config)` returns `IaItemExporter`). Without an `images`
  file it builds `<id>_jp2.zip` from `leaf_images` when every leaf has one.
- The render stage (the orchestrator's half, after `evilflowers_render.book`) ends by writing
  the four IA documents from the rows
  (`IaItemExporter(store).write_item(book, pages)`), so the output tree is a complete IA
  item after every render; `_files.xml` is written last and lists every file. Upload helpers may
emit `ia upload`-compatible metadata (`x-archive-meta-*` with `mediatype=texts`, and
`x-archive-queue-derive:0` when we upload our own derivatives).

## Queues and tasks (Celery)

> **Superseded (#6).** The tasks below are the current ones. The text after them (the
> `interactive` / `cpu` / `ai` / `io` queues, the `studio.*` tasks, liveness, detectors) is the
> first design, kept for history. One queue, `digitalizer.portal`, is consumed by the
> orchestrator (`studio/workers/app.py`); the jobs are in the `jobs` table
> ([architecture.md](../architecture.md#the-job-ledger)).

Orchestrator tasks (queue `digitalizer.portal`; the app is
`evilflowers_books_digitalizer.studio.workers.app:celery`):

| Task | Effect |
|---|---|
| `digitalizer.run` | start a queued run: plan its first stage, then pump |
| `digitalizer.pump` (beat, 15 s) | admit planned jobs within capacity and send them |
| `digitalizer.job_done`, `digitalizer.job_failed` | the `link` and `link_error` callbacks of a service job |
| `digitalizer.reconcile` (beat, 60 s) | settle lost outcomes, dead workers and jobs past their deadline |
| `digitalizer.discover` | scan store to books (status `discovered`) |
| `digitalizer.export_training` | confirmed regions to COCO / DocLayNet JSON and crops |
| `digitalizer.local.clean_marks` | `clean/marks`: book-wide scan-mark groups (#14) |
| `digitalizer.local.title_page` | `analyze/title_page`: metadata suggestions (#15) |
| `digitalizer.local.render_prepare` | `render/prepare`: metadata lookup, text files, the render request |
| `digitalizer.local.gate` | quality gate |
| `digitalizer.local.publish` | catalog publication |

Service tasks (defaults of `DEFAULT_ROUTES`; the services' own queues):

| Stage | Task | Queue |
|---|---|---|
| clean | `evilflowers_scan.clean` | `evilflowers_scan.standalone[.interactive]` |
| analyze | `evilflowers_ocr_worker.standalone.hocr` | `evilflowers_ocr_worker.standalone[.interactive]` |
| analyze | `evilflowers_analyzer.standalone.layout` | `evilflowers_analyzer.standalone[.interactive]` |
| analyze (confirmed `remove` boxes) | `evilflowers_scan.erase` | `evilflowers_scan.standalone[.interactive]` |
| formulas | `evilflowers_equation.standalone.formulas` | `evilflowers_equation.standalone[.interactive]` |
| render | `evilflowers_render.book` | `evilflowers_render.standalone` |

### Historical: the first design

The app is `evilflowers_books_digitalizer.studio.workers.app:celery`, with Redis as both
broker and result backend.

| Queue | Pool | Work |
|---|---|---|
| `interactive` | worker-cpu (prefetch 1, consumed first) | operator re-runs, single pages |
| `cpu` | worker-cpu (`--concurrency` = cores / 2) | clean pages, OCR, render |
| `ai` | worker-ai (concurrency 1–2) | layout detection, VLM jobs, restoration models |
| `io` | worker-io (+ beat) | discover, previews/tiles, publish, export |

| Task (name) | Queue | Args | Effect |
|---|---|---|---|
| `studio.discover` | io | `project, collection=None` | ScanStore → upsert books (status `discovered`), events `book.discovered` |
| `studio.run` | io | `run_id` | orchestrates a Run: builds a chain/chord of the tasks below, marks the run `running` |
| `studio.clean` | cpu / interactive | `run_id, page_indices` | frames → cleaned pages (+ overrides) → previews/tiles; `page.updated` |
| `studio.analyze` | ai / interactive | `run_id, page_index` | OCR + layout detectors → `fuse()` → regions; `regions.updated` |
| `studio.formulas` | ai | `run_id` | VLM on formula/mixed regions (`action = reocr`) |
| `studio.render` | cpu | `run_id` | access pages → PDF, PDF/A-2b, EPUB (Renderer) |
| `studio.gate` | io | `run_id` | QualitySignals → `quality_gate()` → book `review` or `approved` (+ auto-publish when the project allows) |
| `studio.publish` | io | `run_id` | CatalogPublisher (create/update; upload pdf+epub, then delete older acquisitions of those MIME types) → Publication row; `book.published`. `publish.files_base_url` set → run fails before any catalog call |
| `studio.export_training` | io | `project` | confirmed regions → COCO / DocLayNet JSON + crops |
| `studio.recover_runs` | io (beat, every 5 min) | — | `workers/recovery.py`: runs with no broker message and a stale `runs.heartbeat_at` are re-dispatched (started, canvas never sent: `dispatched_at` NULL), re-enqueued (queued) or failed (running). Thresholds: `STUDIO_RECOVER_*` env vars |

**Liveness (migration 0003).** `runs.heartbeat_at` is refreshed by every task at start,
every 60 s while it runs, and at the end; `runs.dispatched_at` is claimed exactly once
before the canvas is sent. Workers need 0003 applied before they start (the `migrate`
init container in `deploy/k8s/base/pod-defaults.yaml`).

**Render input.** `RenderRequest.extra["regions"]` carries one list of region rows per
access page; the EPUB applies confirmed decisions (and proposed remove / as_image /
formula / picture) via `reflow.edition.RegionDecision`, using stored LaTeX instead of the VLM.

**Detectors and formulas.** Layout adapters: `heuristic`, `doclayout` (alias `yolo`;
DocLayout-YOLO ONNX on onnxruntime, settings `[pipeline.layout]`). Formula adapters:
`none`, `paddleocr-vl` (local sidecar), `paddleocr-vl-http` (the `vlm` service,
`POST /v1/formulas`, settings `[pipeline.vlm]`).

Every task:
- emits `run.progress` and `run.log`;
- is idempotent (it re-checks state from the DB) and skips a run that is no longer
  active (cancel sets `runs.status`; revoke cannot stop a task already running);
- marks the run `failed`, with the error, on an exception.

Retries happen only for transient errors (network, DB lock).

**Ordering.** Approving a book records a RENDER run and then a PUBLISH run and enqueues
both on `io`. `studio.run` holds a PUBLISH run (re-tries every 15 s, no retry budget
spent) while an *earlier* FULL or RENDER run of the same book is queued or running
(`stages.orchestrate.blocked_by`). RENDER runs never pass through the gate, so an
approved book stays approved.

**Queues.** Each queue has its own direct exchange and routing key (`Queue(name,
Exchange(name), routing_key=name)`), which is also what `send_task(queue=name)` creates on
the API side. A bare `Queue(name)` would bind all four queues to the default exchange
with one routing key, and Redis would deliver every message to every queue.

**Adapter factories.** `studio/adapters/registry.py` builds a project's adapters by name
(`build_adapters(cfg, project)`); new adapters register with `register(port, name,
factory)`. Other factories: `adapters.artifacts.build_artifact_store(config, project)`,
`adapters.ia.build_ia_exporter(config)`, `adapters.catalog.build_publisher(project.publish,
artifacts_root=config.artifacts_root)`.

## REST API (`/api/v1`, JSON)

There is no authentication (VPN only). Operator identity is the `X-Operator` header, a
free-text name the SPA stores per browser, **percent-encoded** (headers are Latin-1;
the API decodes it with `urllib.parse.unquote`). It is recorded on notes, reviews and
region edits.

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /healthz` (no prefix) | | `{status: "ok"}`; no database or broker round trip (probes) |
| `GET /projects` | | `[ProjectOut]`: slug, name, auto_publish, outputs, `collections {key: name}`, `collection_counts {key: {status: n}}`, `counts {status: n}` |
| `GET /projects/{p}/config` | | `ProjectConfigOut`: the read-only preset (no secrets, only the API key's env var name) |
| `POST /projects/{p}/imports` | `{collection?}` | `202 {task_id}` (enqueues `digitalizer.discover`) |
| `GET /books` | `project, collection?, status?, q?, flagged?, limit, offset, sort` | `{items: [BookOut], total}` |
| `GET /books/{id}` | | `BookDetail`: book, item metadata, quality report, latest runs, publication, artifacts with exists flags, every path |
| `PATCH /books/{id}/metadata` | partial `ItemMetadata` | `ItemMetadata` (validated) |
| `GET /books/{id}/title-page` | | `TitlePageOut`: `suggestions [{field, metadata_field, value, p, line, leaf}]` (fields `title`, `authors`, `publisher`, `year`), `decider_model`, `suggested_at`, `confirmed` (the comparison, or null). Never imported until confirmed (#15) |
| `POST /books/{id}/title-page/confirm` | `{title?, authors?, publisher?, year?}`, `X-Operator` | `TitlePageOut`. Writes the final values to the item metadata (`title`, `creator`, `publisher`, `date`) and stores per field `accepted` / `changed` / `none` in `books.metadata_confirmed`; 400 when there are no suggestions |
| `GET /books/{id}/mark-groups` | | `[MarkGroupOut]`: id, category, band (`keep` / `review` / `hide`), decision, p, source (`signals` / `rule` / `decider` / `operator`), decider_model, leaves, members, crop_leaf, crop_bbox, `on_leaves`, `image_url`, `split_from`. Largest first (#14) |
| `PATCH /mark-groups/{id}` | `{band: keep\|review\|hide}`, `X-Operator` | `MarkGroupOut`. Sets source `operator`; the members' marks change at once, their regions at the next analyze of those leaves |
| `POST /mark-groups/{id}/split` | `{leaves: [int]}` (at least one), `X-Operator` | `201 MarkGroupOut` (the new group, verdict `review`, `split_from` = the old one); 400 when no occurrence is on those leaves or none would stay |
| `GET /mark-groups/{id}/image` | | PNG crop of the group's clearest occurrence (max side 640) |
| `POST /books/{id}/runs` | `{kind: full\|rerun\|render\|publish, pages?: [int], interactive?: bool}` | `202 RunOut` (creates the run and enqueues `digitalizer.run`) |
| `GET /runs/{id}` | | `RunOut` (status, stage, done/total, log tail) |
| `POST /runs/{id}/cancel` | | `RunOut` |
| `GET /books/{id}/pages` | | `[PageOut]` (index/leaf, page type, side, printed number, status, version, dims, thumbs) |
| `GET /pages/{id}` | | `PageDetail`: page, regions, notes, override, OCR text, source frame path + crop |
| `PATCH /pages/{id}` | `{page_type?, page_number?, include_in_access?, override?}` | `PageDetail` |
| `POST /pages/{id}/regions` | `{kind, bbox, action?, text?}` | `RegionOut` (source operator, status confirmed) |
| `PATCH /regions/{id}` | `{kind?, bbox?, status?, action?, text?}` | `RegionOut` |
| `DELETE /regions/{id}` | | `204` (sets status rejected; never hard-deletes) |
| `POST /notes` | `{book_id?, page_id?, region_id?, text}` | `NoteOut` |
| `POST /books/{id}/reviews` | `{decision, note?}` | `ReviewOut`; `approved` enqueues render+publish when the project auto-publishes after review |
| `GET /events` | SSE, `project?, book?` | event stream (see `studio/events.py`) |
| `GET /pages/{id}/image/{layer}` | `layer=original\|spread\|clean\|pdf; w` | JPEG/WebP (cached) |
| `GET /pages/{id}/dzi` (alias `dzi.xml`) / `GET /pages/{id}/dzi_files/{level}/{col}_{row}.{ext}` | `layer=clean\|original\|spread`, `v` | Deep Zoom (OpenSeadragon). Clients open `dzi.xml?v=…`: only with an extension does OSD keep the query string for tile URLs |
| `GET /books/{id}/files/{kind}` | | the artifact (`pdf`, `pdfa`, `epub`, `meta_xml`, `scandata_xml`, …) |
| `GET /books/{id}/export/ia` | | a zip in IA item layout (meta.xml, scandata.xml, page_numbers.json, hocr, page images) |
| `GET /scheduler` | | `SchedulerOut`: `mode` (`normal`, `peak`, `contended`, `paused`), `reason`, `lanes` and `queues` (max jobs in flight), `held_stages`, `peak_hours`, `timezone`, `jobs` (open jobs by status and lane) |
| `POST /scheduler/pause` | optional `{reason?}`, `X-Operator` | `SchedulerOut`: holds bulk jobs (Redis key `digitalizer:scheduler:paused`); interactive jobs are never held |
| `POST /scheduler/resume` | | `SchedulerOut` |
| `GET /labelling/tasks` | | `[TaskOut]`: name, title, label keys + hints, target, items, labelled, skipped, double, mine (#7). Tasks: `figure_kind`, `is_data_table`, `table_quality`, `section_relation`, `subject`, `scan_mark` (item kind `mark_group`, from the studio DB), `title_page` (item kind `book`, `by_hand = false`: progress and export only) |
| `POST /labelling/tasks/{task}/samples` | query `n` (1–2000, default 100) | `202 {task, n, status}`: samples up to `n` new items in the background, from the catalog or, for `scan_mark`, from the studio DB. 503 when `[labelling]` is not configured (catalog tasks only; `scan_mark` needs none). `title_page` is not sampled: its cases come from confirmed forms |
| `GET /labelling/tasks/{task}/next` | `X-Operator` required | `LabelItemOut \| null`: payload only, never the model's answer (band, p, decision, relation). Order: a spot check of an agent label until 10 % of agent-labelled items have a human answer, then a second-person item until 10 %, then a fresh item that nobody answered and no agent holds (#16) |
| `GET /labelling/items/{id}` | | `LabelItemOut` |
| `POST /labelling/items/{id}/labels` | `{labels: [{task, value}]}`, `X-Operator` required | `LabelItemOut`; one answer per (item, task, labeller), `skip` allowed |
| `GET /labelling/items/{id}/image` | | the crop: proxied from the catalog (the key stays on the server), or cut from the cleaned page for a mark group |
| `GET /labelling/tasks/{task}/export` | query `labeller_kind` (`human` \| `agent`, optional; 400 otherwise) | JSONL, one line per non-skip label, with `labeller_kind` and `model` (#16); for `title_page`, one line per field of each confirmed form |
| `GET /labelling/tasks/{task}/summary` | | counts per label / language / labeller / via / labeller_kind / model; `human_human` and `agent_human` agreement (pairs, agreement, κ, per language, confusion); `double`/`agreement`/`kappa` = human↔human; spot-check progress; `agent_trusted` (κ ≥ 0.7 once the spot check is complete); sampling note |
| `POST /mcp` (no prefix) | MCP streamable HTTP (JSON-RPC) | labelling tools: `list_label_tasks`, `next_label_item` (text + crop image), `submit_label`, `sample_label_items`, `label_summary`, `export_labels`; with `[labelling] agent_labellers = true` also `claim_label_items`, `submit_labels`, `release_label_items` for a model labelling on its own (#16); also `studio mcp` over stdio |

Errors are `{"detail": str}`, with status codes 400/404/409 (state transition) and 422.

**DTO shapes the SPA relies on** (`studio/application/dto.py` ↔ `frontend/src/api/types.ts`):

- `BookOut`: id, project, collection, external_id, slug, item_id, title, status,
  quality_score, flags, `flagged` (status `review` or flags present), n_pages, n_frames,
  updated_at, latest_run, `step` + `progress` (0..1) of the active run.
- `BookDetail`: book, `item_metadata` (`ItemMetadata.as_dict()`), quality, runs (newest
  first), publication(s), reviews, notes, `artifacts [{kind, path, exists, size, url}]`,
  `paths {name: path | null}`, page counts.
- `PageDetail`: page, regions, notes, override, leaf_extra, ocr_text, ocr_conf,
  `frame {path, exists, width, height, crop_box [x,y,w,h], rotate_degree, skew_angle,
  side, n_sides, x0, x1}`, `prev_id`, `next_id`, files, images.
- Run log lines are `{ts, level, message}`.

## SPA (frontend/)

The stack is React 18, TypeScript, Vite, TanStack Query, React Router, OpenSeadragon and
Annotorious (`@annotorious/openseadragon`). `@techstark/opencv-js` (WASM) is lazy-loaded
for live previews. CSS modules with design tokens, light and dark. No UI kit is mandatory;
keep it light.

**Screens**

1. **Dashboard.** Projects and collections with segmented progress bars
   (discovered/queued/running/review/approved/published/error). It also has a live activity
   feed (SSE), the running runs with their stage bars, and import buttons.
2. **Books table.** Filters (status, flagged, collection, search), sort by quality score, and
   bulk actions (queue run, approve).
3. **Book.** Metadata form (`ItemMetadata`, IA field names, OPAC lookup button); quality
   flags; runs with a live log; a pages grid (page type badges, disputed markers);
   review/approve; publication history; and downloads. Added since: a **Scan marks** panel
   (keep / hide / split a book-wide mark group, #14) and a **Title page** panel (the suggested
   title, authors, publisher and year, confirmed by the operator, #15).
4. **Page studio** (the core).
   - **Viewer.** OpenSeadragon deep zoom with layers (original frame half, whole spread,
     cleaned page, PDF render), side-by-side or swipe compare, and synced viewports.
   - **Regions.** Annotorious rectangles coloured by kind, dashed when disputed. Draw,
     resize, relabel, set the action (keep/remove/as image/re-OCR) and add notes.
     Keyboard: `t` text, `f` formula, `b` table, `p` picture, `s` stamp, `x` reject,
     `Enter` confirm, `←/→` page.
   - **Disputed regions** are listed with their reason; accept or relabel in one click.
   - **Page properties.** Page type (IA pageType), printed page number, include in access,
     and overrides (crop, deskew, split, despeckle, colour mode, OCR language). OpenCV.js
     previews crop, deskew and threshold instantly before a re-run.
   - **Text & math.** The OCR text per region is editable. Formula LaTeX is edited with a
     live KaTeX/MathML preview.
   - **Re-run page** (interactive lane). The tile swaps when `page.updated` arrives.
5. **Review queue.** Flagged books sorted by score, keyboard-driven (`j/k`, `a` approve,
   `o` open).
6. **Settings.** Operator name (local), project presets (read-only view of the config).

All live data comes from SSE events, which invalidate TanStack Query caches. There is no
polling, except as a fallback when SSE is down.

## Testing the whole system locally

1. Start the dev stack.
2. Run the API:
   `uvicorn evilflowers_books_digitalizer.studio.api.app:create_app --factory`.
3. Run the orchestrator:
   `celery -A evilflowers_books_digitalizer.studio.workers.app:celery worker -Q digitalizer.portal --beat`
   (or `evilflowers-digitalizer studio worker -- -B`), and the service workers you need from
   their repositories, on the same Redis. See
   [deploy/dev/README.md](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/blob/master/deploy/dev/README.md).
4. Serve the SPA with `pnpm dev`, which proxies `/api` to the API.
5. Config: `configs/studio.dev.toml`, with project `stu-local`; its scans must be under the
   storage root (`.data`).
