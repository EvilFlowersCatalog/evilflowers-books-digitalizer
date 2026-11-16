# Digitalizer on k3s (namespace `digitalizer`)

Design: `docs/design/target-architecture.md`, diagrams in `docs/diagrams/`. One image
(`ghcr.io/evilflowerscatalog/evilflowers-books-digitalizer`) runs the portal and the
orchestrator; the command decides the role. It carries no OCR, cleaning, layout, formula or
rendering engine (evilflowers-books-digitalizer#6): those jobs run on the service workers on
Valkey db 8, deployed with their own images by the knowledge repository's Ansible role
(knowledge#66).

| Deployment | Role | Replicas | Requests → limits |
|---|---|---|---|
| `portal` | uvicorn: REST, SSE, tiles, SPA, `/mcp`, `/api/v1/scheduler` | 1 (Recreate) | 100m/512Mi → 1/2Gi |
| `orchestrator` | Celery `digitalizer.portal` + beat: runs, job callbacks, pump (15 s), reconcile (60 s), discover, the local steps (leaf planning, `clean/marks`, `title_page`, `render/prepare`, gate, publish); ledger `/metrics` on :9464 | 1 (Recreate) | 200m/768Mi → 1/2Gi |

Every pod runs the init container `migrate` (`alembic upgrade head`, `pod-defaults.yaml`),
so nothing starts against an older schema.

## The service workers (knowledge#66)

Second Deployments of the services' own images, on Valkey db 8, each scaled 0..N by KEDA on
its queue (plus one replica for its `.interactive` queue), under PriorityClass
`digitalizer-batch` (value −100, never preempting). The task names and queues are the
defaults of `studio.config.DEFAULT_ROUTES`:

| Image | Queues |
|---|---|
| evilflowers-scan-service | `evilflowers_scan.standalone[.interactive]` |
| evilflowers-ocr-service | `evilflowers_ocr_worker.standalone[.interactive]` |
| evilflowers-analyzer-service | `evilflowers_analyzer.standalone[.interactive]` |
| evilflowers-equation-service | `evilflowers_equation.standalone[.interactive]` (`LATEX_BACKEND=paddleocr-vl`) |
| evilflowers-render-service | `evilflowers_render.standalone` |

They mount the same storage as the portal (their `STORAGE_PATH` = the studio's storage root)
and need no catalog key.

## Who yields to whom

Book digitalization is background work; the catalog's own services and the operator
come first.

1. **Admission** (`[scheduling]` in `configs/studio.k8s.toml`). The orchestrator's pump
   sends bulk jobs only within the current capacity:
   * `normal`: `bulk_inflight` jobs;
   * `peak`: `bulk_inflight_peak` jobs (mon–fri 07–19 by default), and the
     `off_peak_stages` (render) are held until the window ends;
   * `contended`: the catalog's queues on Redis db 7 are deeper than `max_depth`;
   * `paused`: an operator switch (`POST /api/v1/scheduler/pause`, Redis key
     `digitalizer:scheduler:paused`).

   Interactive re-runs have their own lane and are never held.
2. **Scaling and node pressure** are the service workers' (KEDA, PriorityClass): admission
   keeps their queues short, so at peak they scale to zero on their own. An evicted job
   goes back to the queue (`acks_late`); the ledger's reconcile re-sends a job with no
   outcome by its deadline, up to its budget.

## Decision services (knowledge#77)

`clean/marks` asks decider-vision once per uncertain group of scan marks (#14);
`title_page` asks decider one question per metadata field (#15). Both need the NetworkPolicy
of namespace `shared` to admit `digitalizer` and `DECIDER_URL` / `DECIDER_VISION_URL` on the
orchestrator. Until then a call fails after the 5 s connect timeout (`DECIDER_TIMEOUT` bounds
the answer), each step stops asking after its first failure, and both fall back (marks stay
`review` and are asked again on the next run, no prefill): a run never fails on them.

## Observability

* Pods carry `elvira.stuba.sk/instrumentation: otel-sdk`. The OTel SDK (extra `telemetry`,
  installed in the image) exports when `OTEL_EXPORTER_OTLP_ENDPOINT` is set
  (`pod-defaults.yaml`). `service.name` is the pod's `app.kubernetes.io/name`
  (`digitalizer-portal`, `digitalizer-orchestrator`).
* Spans carry `digitalizer.run.id`, `digitalizer.job.id` and `digitalizer.stage`; the same
  ids appear in the JSON logs. Celery messages carry `digitalizer_*` headers, so a service
  worker can tag its spans.
* The `orchestrator` serves `/metrics` from its Celery main process
  (`DIGITALIZER_METRICS_PORT=9464`, `studio/ledger_metrics.py`): `digitalizer_jobs`,
  `digitalizer_job_oldest_planned_seconds`, `digitalizer_runs`, `digitalizer_books`,
  `digitalizer_scheduler_capacity`, `digitalizer_scheduler_mode`, and for the decisions
  `digitalizer_marks{band}`, `digitalizer_mark_groups{source,band}`,
  `digitalizer_mark_decider_calls_saved`, `digitalizer_title_page_fields{field,outcome}`.
* Logs are one JSON object per line on stderr (`EVILFLOWERS_LOG_FORMAT=json`).

Service `portal` (ClusterIP :80) is VPN-only: no HTTPRoute.
URL: `http://portal.digitalizer.stu.k8s.internal`.

## Render (never apply by hand)

```bash
kubectl kustomize --load-restrictor=LoadRestrictionsNone deploy/k8s/overlays/dev > /tmp/digitalizer-dev.yaml
```

The flag is needed because the ConfigMap is generated from `configs/studio.k8s.toml`,
which lives outside this directory. The Ansible role drops the rendered file into
`/var/lib/rancher/k3s/server/manifests/` (Cookbooks/Deploy-a-Service §2).

## Prerequisites (knowledge#66, #77)

1. **NFS on gateway**: `raw-scans` (ro) and `digitized` (rw), mounted under the **storage
   root** (`STUDIO_STORAGE_ROOT` in the overlay's `studio-env`, dev: `/data`;
   `components/storage-root` moves the mounts there). The service workers mount the same
   exports with the same layout below their `STORAGE_PATH`.
2. **Workload label** `elvira.stuba.sk/workload=apps` on the nodes.
3. **Postgres and Valkey** in namespace `shared`: the `digitalizer` database and role;
   Valkey db 8 as the broker (the `STUDIO_REDIS_URL` secret); read access to gateway's
   Redis db 7 (the catalog's queues, for backpressure).
4. **The service workers** above, KEDA and the PriorityClass `digitalizer-batch`.
5. **Secrets** in namespace `digitalizer`: `ghcr-pull` and `studio-secrets`
   (`base/secret.example.yaml`).
6. **Decision services** admitted for namespace `digitalizer` (knowledge#77).

**Publication is optional.** A project without a `[publish]` target never publishes: an
approved book is complete, with its files (PDF, PDF/A-2b, EPUB, IA item) on the digitized
export.

## Verify (VPN on)

```bash
STUDIO_BASE_URL=http://portal.digitalizer.stu.k8s.internal deploy/dev/smoke.sh
curl -s http://portal.digitalizer.stu.k8s.internal/api/v1/scheduler
```
