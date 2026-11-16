# Deployment

The digitalizer is a **portal** (API + SPA) and an **orchestrator** (the job ledger). Every
digitalization job runs on a service worker on the shared broker. The batch pipeline and its
run-to-completion deployments (screen, systemd, Docker Compose, the k8s Job) were retired in
evilflowers-books-digitalizer#6.

The manifests and the cluster-specific facts are the source of truth and are not repeated
here:

- [deploy/README.md](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/blob/master/deploy/README.md):
  the stage-to-service table and the environment variables;
- [deploy/k8s/README.md](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/blob/master/deploy/k8s/README.md):
  Deployments, the service workers (knowledge#66), admission, decision services (knowledge#77),
  observability, prerequisites;
- [deploy/dev/README.md](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/blob/master/deploy/dev/README.md):
  the local stack.

This page is the runtime contract of the image.

## Roles

One image, two roles. The container command picks the role.

| Role | Command | Notes |
|---|---|---|
| portal | `uvicorn evilflowers_books_digitalizer.studio.api.app:create_app --factory --host 0.0.0.0 --port 8000` | REST, SSE, tiles, `/mcp`, the SPA. `STUDIO_ROLE=api` arms the Docker `HEALTHCHECK`. |
| orchestrator | `celery -A evilflowers_books_digitalizer.studio.workers.app:celery worker --beat -Q digitalizer.portal -n orchestrator@%h` | Runs, job callbacks, pump, reconcile, discover and the local steps. **Exactly one replica**: beat is embedded. |

The CLI equivalents are `evilflowers-digitalizer studio serve` and
`evilflowers-digitalizer studio worker -- -B`.

Every pod runs an init container, `migrate` (`alembic upgrade head`), so nothing starts
against an older schema. Both Deployments use the `Recreate` strategy, one replica.

## Environment variables

| Variable | Meaning |
|---|---|
| `STUDIO_CONFIG` | path to the studio TOML. Precedence: `--config`, this variable, `configs/studio.toml` of a checkout |
| `STUDIO_DATABASE_URL`, `STUDIO_REDIS_URL` | database and broker; they override `[studio]` |
| `STUDIO_STORAGE_ROOT` | where this pod mounts the shared storage (the services' `STORAGE_PATH`); overrides `[studio] storage_root`. Required, directly or in the file. |
| `STUDIO_STATIC_DIR` | the SPA bundle (the image sets `/app/frontend/dist`) |
| `STUDIO_RENDER_CONCURRENCY` | concurrent tile and rendition builds in the portal (default 2) |
| `STUDIO_OPERATOR` | the operator name for the CLI labelling commands |
| `DECIDER_URL`, `DECIDER_VISION_URL`, `DECIDER_MODEL`, `DECIDER_TIMEOUT` | the decision services; win over `[decider]` |
| `DECIDER_ENABLED` | `false` switches both decision uses off |
| `DIGITALIZER_PUMP_INTERVAL_SECONDS`, `DIGITALIZER_RECONCILE_INTERVAL_SECONDS` | beat intervals (15 and 60 s) |
| `DIGITALIZER_METRICS_PORT` | port of the orchestrator's `/metrics` (k8s: 9464) |
| `EVILFLOWERS_API_KEY` | catalog token; the name is `[projects.<p>.publish] api_key_env` |
| `EVILFLOWERS_LABELLING_API_KEY` | catalog key for labelling; the name is `[labelling] api_key_env` |
| `EVILFLOWERS_LOG_FORMAT` | `text` (default) or `json` |
| `EVILFLOWERS_LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARN`/`WARNING`, `ERROR`, `CRITICAL` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | enables the OpenTelemetry SDK (extra `telemetry`, installed in the image) |

`EVILFLOWERS_DIGITALIZER_CONFIG` and the `STUDIO_RECOVER_*` variables of earlier versions are
gone.

**Paths** in the TOML: `{storage_root}` expands to the storage root; other relative paths
resolve against the directory of the config file. Every scan root and `artifacts_root` must
be below the storage root, or `load_config` fails.

## Configuration

A full annotated example is the docstring of `evilflowers_books_digitalizer/studio/config.py`;
the shipped files are `configs/studio.dev.toml` and `configs/studio.k8s.toml`.

| Table | Purpose |
|---|---|
| `[studio]` | `database_url`, `redis_url`, `storage_root`, `artifacts_root`, `cache_dir` |
| `[projects.<slug>]` | `scans_root`, `book_pattern`, `frames_glob`, `item_id`, `language`, `ocr_language`, `outputs`, `auto_publish` (`never`, `gate`, `review`), `collections` |
| `[projects.<slug>.metadata]` | `providers`, `excel_path`, `columns`; see [Metadata](metadata.md) |
| `[projects.<slug>.publish]` | `target` (`none` or `evilflowers`), `base_url`, `api_key_env`, `catalog`. Publication is optional: without a target an approved book is complete. |
| `[projects.<slug>.quality]` | gate policy, for example `min_ocr_conf` |
| `[projects.<slug>.pipeline.scantailor]` | options sent to scan-service with every clean job (`output_dpi` is the item's ppi) |
| `[orchestration]` | `budget`, `stale_after_seconds`, `start_grace_seconds`, and `[orchestration.stages.<stage>]` route overrides (`task`, `queue`, `interactive_queue`, `deadline_seconds`, `batch`, `interactive_batch`). `analyze` takes subtables `hocr`, `layout`, `erase`, `title_page`; `render` takes `prepare`, `book`; `clean` takes `marks`. |
| `[scheduling]` | peak hours, in-flight limits, `off_peak_stages`, `queue_inflight`, `[[scheduling.backpressure]]` |
| `[decider]` | `url`, `vision_url`, `model`, `timeout`; an empty URL turns that use off |
| `[marks]` | `min_leaves`, `hash_distance`, `aspect_tolerance`, `hide_above`, `keep_above` |
| `[title_page]` | `first_leaves`, `last_leaves`, `max_candidates` |
| `[labelling]` | `base_url`, `api_key_env`, `catalog_ids`, `max_entries`, `timeout`, `seed` |

Rejected at load time (they configured the retired in-process stages):

- the project keys `layout_detectors` and `formula_recognizer`;
- the `[pipeline]` keys `tesseract`, `heuristic`, `layout`, `vlm`, `reflow`, `docres`,
  `chunk_size`;
- `backend = "local"` on clean, analyze, formulas or render.

Unknown keys in `[marks]`, `[title_page]`, `[decider]`, `[labelling]` and in a stage table are
errors too.

## Probes and ports

| Role | Endpoint | Use |
|---|---|---|
| portal | `GET /healthz` on :8000 | startup, readiness and liveness. It answers `{"status": "ok"}` without a database or broker round trip. |
| orchestrator | `celery ... inspect ping -d orchestrator@$HOSTNAME` | liveness (exec probe) |
| orchestrator | `GET /metrics` on `$DIGITALIZER_METRICS_PORT` | Prometheus gauges |

The portal has no `/metrics`: the ledger gauges are served by the orchestrator, so an alert on
them keeps working while the portal is down.

## Metrics

Served by the orchestrator's Celery main process (`studio/ledger_metrics.py`):

| Metric | Meaning |
|---|---|
| `digitalizer_jobs{stage,lane,status}` | open jobs of active runs |
| `digitalizer_job_oldest_planned_seconds{lane}` | how long the oldest planned job waits |
| `digitalizer_runs{kind,status}` | queued and running runs |
| `digitalizer_books{project,status}` | books per lifecycle status |
| `digitalizer_scheduler_capacity{lane}`, `digitalizer_scheduler_mode{mode}` | admission: jobs admitted in flight, and 1 for the current mode |
| `digitalizer_ledger_scrape_ok` | 0 when the ledger could not be read |
| `digitalizer_marks{band}`, `digitalizer_mark_groups{source,band}` | scan marks and book-wide mark groups (#14) |
| `digitalizer_mark_decider_calls_saved` | decider-vision calls the grouping saved |
| `digitalizer_title_page_fields{field,outcome}` | title-page suggestions by operator outcome (#15) |

## Logs

Logs go to **stderr**. With `EVILFLOWERS_LOG_FORMAT=json`, every line is one JSON object:

```json
{"time": "2026-11-16T09:12:44.310Z", "level": "WARN", "logger": "evilflowers_books_digitalizer.studio.workers.stages.marks",
 "msg": "decider-vision: group 8 not judged: decider http://decider-vision.shared.svc.cluster.local/v1/systemone: ConnectTimeout: ",
 "project": "stu", "run_id": 17, "job_id": 212, "stage": "clean"}
```

- Always: `time` (RFC 3339, UTC), `level` (`DEBUG`, `INFO`, `WARN`, `ERROR`, `CRITICAL`),
  `logger`, `msg`.
- When known: `project`, `book_pk`, `run_id`, `job_id`, `stage`, `lane`, `task_id`,
  `duration_ms`, `status`, `entry_id`, `trace_id`, `span_id`.
- On exceptions: `exc_type` and `exc` (the traceback).

The ledger ids match the span attributes `digitalizer.run.id`, `digitalizer.job.id` and
`digitalizer.stage`, and Celery messages carry `digitalizer_*` headers, so a service worker can
tag its spans with the same ids.

## The image

`ghcr.io/evilflowerscatalog/evilflowers-books-digitalizer`, built by
`.github/workflows/docker.yml` from `Dockerfile`:

- base `python:3.12-slim-trixie`; the SPA is built in a separate node stage and only
  `frontend/dist` is copied in;
- the Python package with the `telemetry` extra; `evilflowers-protocol` is installed from its
  public git repository at the commit in the `PROTOCOL_REF` build argument;
- no ScanTailor, Tesseract, jbig2enc, recode_pdf, pandoc, DocRes or ONNX runtime. The build
  fails if `onnxruntime`, `tesseract` or `scantailor-deviant-cli` is present;
- default `USER 10001`, `HOME=/tmp`; any non-root uid:gid works, nothing writes under `/app`
  or `/opt`;
- `evilflowers-digitalizer` is on `PATH`; the default command prints `--help`.

The orchestrator's own pixel work is spread detection (OpenCV); the portal's renditions use
Pillow and pypdfium2.

## Shared storage and the services

The service workers (scan, ocr, analyzer, equation, render) are deployed from their own
images by the knowledge repository (knowledge#66). They must:

- be on the same broker (Valkey db 8 in the cluster);
- mount the same storage, with their `STORAGE_PATH` at the layout of the digitalizer's storage
  root (`raw-scans/`, `digitized/`);
- declare the task names and queues of `DEFAULT_ROUTES` (or the project overrides them).

Each job's message carries `run_id`, `job`, `token`, `inputs`, `output_dir` and `outcome`
(paths relative to the storage root). The service writes its output, then the outcome file
last. See [Architecture](architecture.md#the-job-ledger).

## Open work

Deployment of the service workers and a parity run on a real book are the remaining part of
evilflowers-books-digitalizer#4 (knowledge#66). Until the services run, a run stays `running`
with its jobs `dispatched`.
