# Operations runbook

This page covers running the portal and the orchestrator. The service workers are operated
from their own repositories (deployed by knowledge#66). The batch runbook (`monitor`, `stats`,
`run-corpus`, resuming a batch) is retired; see [Archive](archive/digitalization_pipeline_report.md).

## Monitoring

| What | Where |
|---|---|
| Books, runs, stage progress, live log | the SPA (Dashboard, Books, Book). Everything updates over SSE (`GET /api/v1/events`). |
| Admission mode and capacity | `GET /api/v1/scheduler` |
| Ledger gauges | the orchestrator's `/metrics` on `$DIGITALIZER_METRICS_PORT` (k8s: 9464). Names are in [Deployment](deployment.md#metrics). |
| Logs | stderr of both pods; `EVILFLOWERS_LOG_FORMAT=json` gives one object per line with `run_id`, `job_id`, `stage` and `trace_id` ([format](deployment.md#logs)) |
| Traces | the OpenTelemetry SDK exports when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; spans carry `digitalizer.run.id`, `digitalizer.job.id`, `digitalizer.stage` |

Metrics worth alerting on:

- `digitalizer_job_oldest_planned_seconds` rising while `digitalizer_scheduler_mode` is
  `normal`: jobs wait but are not admitted, or no worker takes them;
- `digitalizer_ledger_scrape_ok == 0`: the orchestrator cannot read the ledger;
- `digitalizer_jobs{status="dispatched"}` not falling: a service is not consuming its queue.

## Holding and releasing bulk work

```bash
curl -X POST http://<portal>/api/v1/scheduler/pause     # hold bulk jobs; running jobs finish
curl -X POST http://<portal>/api/v1/scheduler/resume
```

The pause is a Redis key (`digitalizer:scheduler:paused`). Interactive re-runs are never held.
At peak hours (`[scheduling] peak_hours`) the capacity narrows and `off_peak_stages` (render)
wait until the window ends. When the queues of a `[[scheduling.backpressure]]` source are
deeper than its `max_depth`, the mode is `contended` and `bulk_inflight_contended` applies.

## Resuming and recovery

A run is a set of ledger jobs, so it survives a restart of the portal or the orchestrator.

- **A worker dies or a callback is lost.** `digitalizer.reconcile` (every 60 s) applies an
  outcome file that exists without a callback. A job with no outcome by its deadline goes back
  to `planned` with a new token, up to `[orchestration] budget` times (the run log says
  `job N lost; re-sending`). After that the job and its run fail.
- **A local job loses its worker.** A local job with no heartbeat for `stale_after_seconds`
  is treated as lost in the same way.
- **A run fails.** Start another run of the same book. Operator decisions (confirmed and
  rejected regions, overrides) are kept.
- **Cancel.** `POST /api/v1/runs/{id}/cancel` (or the Cancel button). The run is marked
  `cancelled`, and the reconcile task revokes the open jobs of ended runs. A task already
  running cannot be stopped, and its outcome is refused.
- **Deadlines** are per stage (`deadline_seconds`): clean and analyze 3600 s, formulas 4 h,
  render 8 h by default. The clock starts at dispatch, so keep render's queue short
  (`[scheduling.queue_inflight]` in `configs/studio.k8s.toml` allows one render job in flight).

## Disk

- The artifacts under `{artifacts_root}/<project>/<item_id>/` are the product. Keep them on
  durable storage and back them up.
- A job writes into its own attempt directory,
  `<item_id>_studio/jobs/<job id>-a<n>/`. The orchestrator deletes the attempt directories it
  has consumed or that can no longer count (lost attempts, superseded runs).
- `cache_dir` is scratch (viewer renditions, tiles, label crops). It can be evicted. A
  render-only run then fails with "access leaves without cleaned image"; run a full run or
  re-run those pages.

## Common issues

| Symptom | Likely cause / fix |
|---|---|
| A run stays `queued` | The orchestrator is not running, or no beat. The pump starts a queued run after `start_grace_seconds` (30 s). Check the `orchestrator` pod and `celery ... inspect active_queues`. |
| A run stays `running`, jobs `dispatched` | No service worker consumes the job's queue, or the worker is on another Redis db. Check the service's queue (`DEFAULT_ROUTES`) and its broker URL. |
| Jobs stay `planned` | Admission: the mode is `peak`, `contended` or `paused` (`GET /api/v1/scheduler`), or `queue_inflight` caps the queue. |
| Run log says `job N lost; re-sending` | The worker died or timed out. Check the service's logs; raise `deadline_seconds` if the work is just slow. |
| Run fails with "no result and no re-sends left" | The budget is spent. Fix the service, then start a new run. |
| `studio serve` or the worker refuses to start with a config error | A removed key (`layout_detectors`, `formula_recognizer`, a retired `[pipeline]` key), a missing storage root, or a scan or artifacts path outside it. The message names the key. |
| Outcome refused (token or run mismatch) in the log | A superseded or cancelled attempt finished late. Harmless; the current attempt counts. |
| Scan marks all stay `review` | The vision decider is off or failing (`[decider] vision_url`, `DECIDER_ENABLED`), or `[marks] hide_above` / `keep_above` are unset (they are, until knowledge#65 calibrates `scan_mark`: every decider verdict is then `review`). Groups on `min_leaves` leaves or more are hidden by rule regardless. |
| No "Title page" panel on a book | The text decider is off or failed, or the run touched no title or colophon leaf, or the operator already confirmed. Nothing is prefilled then; the run still succeeds. |
| Render fails with "access leaves without cleaned image" | `cache_dir` was evicted: run a full run or re-run those leaves. |
| Publish fails at once with a `files_base_url` error | `publish.files_base_url` is set; the catalog cannot create URL-backed files yet. Remove the key. |
| Tiles do not change after a re-run | Hard-reload; tile URLs carry `?v=<page version>`. |

## Schema migrations

```bash
evilflowers-digitalizer studio migrate        # or: db upgrade
evilflowers-digitalizer db current
```

In k8s, the `migrate` init container runs `alembic upgrade head` in every pod. The head
revision is `0007_marks_title_page` (table `mark_groups`; columns `books.metadata_suggestions`,
`books.metadata_confirmed`).

## Publishing

Publication is optional and runs as a `publish` run. See
[Studio guide, Approve and publish](studio.md#6-approve-and-publish). The catalog token is read
from the environment variable named in `[projects.<p>.publish] api_key_env`.
