# Digitalizer on k3s (namespace `digitalizer`)

Design: `docs/design/target-architecture.md`, diagrams in `docs/diagrams/`. One image
(`ghcr.io/evilflowerscatalog/evilflowers-books-digitalizer`) runs the portal and every
Celery role; the command decides the role. PaddleOCR-VL (formulas) is **not** deployed
here: the shared `vlm` service (namespace `shared`, evilflowers-vlm-service) is called over
HTTP.

| Deployment | Role | Replicas | Priority | Requests → limits |
|---|---|---|---|---|
| `portal` | uvicorn: REST, SSE, tiles, SPA, `/api/v1/scheduler` | 1 (Recreate) | default | 100m/512Mi → 1/2Gi |
| `orchestrator` | Celery `digitalizer.portal` + beat: runs, job callbacks, pump (15 s), reconcile (60 s), discover, gate, publish; ledger `/metrics` on :9464 | 1 (Recreate) | default | 200m/768Mi → 1/2Gi |
| `worker-interactive` | Celery `digitalizer.interactive`: operator re-runs, concurrency 2 | 1 | default | 500m/2Gi → 2/6Gi |
| `worker-cpu` | Celery `digitalizer.cpu`: bulk clean / render, concurrency 2 | **0..3 (KEDA)** | `digitalizer-batch` | 500m/2Gi → 2/6Gi |
| `worker-ai` | Celery `digitalizer.ai`: bulk layout / formulas, concurrency 1 | **0..1 (KEDA)** | `digitalizer-batch` | 500m/2Gi → 2/6Gi |

Every pod runs the init container `migrate` (`alembic upgrade head`, `pod-defaults.yaml`),
so nothing starts against an older schema.

## Who yields to whom

Book digitalization is background work; the catalog's own services and the operator
come first. Three layers, each one configuration:

1. **Admission** (`[scheduling]` in `configs/studio.k8s.toml`). The orchestrator's pump
   sends bulk jobs only within the current capacity:
   * `normal`: `bulk_inflight` jobs;
   * `peak`: `peak_bulk_inflight` jobs (mon–fri 07–19 by default), and the
     `off_peak_stages` (render) are held until the window ends;
   * `contended`: the catalog's queues on Redis db 7 are deeper than `max_depth`;
   * `paused`: an operator switch (`POST /api/v1/scheduler/pause`, Redis key
     `digitalizer:scheduler:paused`).

   Interactive re-runs have their own lane and are never held.
2. **Scaling**. KEDA (`overlays/dev/keda.yaml`) scales the bulk workers on their queue's
   length on the broker (Valkey `valkey.shared`, db 8). Admission keeps that queue short, so at peak the workers scale
   to zero on their own. Scale-in is slow: there is a 600 s grace for the running chunk.
3. **Node pressure**. The bulk workers run under PriorityClass `digitalizer-batch`
   (`base/priorityclasses.yaml`, value −100, `preemptionPolicy: Never`): they never
   preempt anything and are evicted first. An evicted job goes back to the queue
   (`acks_late`). The ledger's reconcile then re-sends a job whose heartbeat went stale,
   up to its budget.

Sizing on control (6 vCPU): 0.8 CPU is always requested (portal, orchestrator,
interactive). Full bulk scale adds 2.0 CPU (3 × cpu + 1 × ai).

## Observability

* Pods carry `elvira.stuba.sk/instrumentation: otel-sdk`. The OTel SDK (extra `telemetry`,
  installed in the image) exports when `OTEL_EXPORTER_OTLP_ENDPOINT` is set
  (`pod-defaults.yaml`).
* `service.name` comes from `OTEL_SERVICE_NAME`, which is the pod's
  `app.kubernetes.io/name` (for example `digitalizer-portal` or
  `digitalizer-worker-cpu`).
* Spans carry `digitalizer.run.id`, `digitalizer.job.id` and `digitalizer.stage`. The
  same ids appear in the JSON logs (`run_id`, `job_id`, `stage`, plus `trace_id`).
* Celery messages carry `digitalizer_*` headers, so a service worker can tag its spans.
* The `orchestrator` serves the ledger's `/metrics` from its Celery main process
  (`DIGITALIZER_METRICS_PORT=9464`, port `metrics`, `studio/ledger_metrics.py`), scraped
  through the `k8s.grafana.com/scrape` annotations. Its gauges include
  `digitalizer_jobs{status,lane,stage}`, `digitalizer_job_oldest_planned_seconds`,
  `digitalizer_runs`, `digitalizer_books`, `digitalizer_scheduler_capacity` and
  `digitalizer_scheduler_mode`. They come from the component that owns the ledger, so they
  survive a portal outage; the portal serves no `/metrics` (#13).
* Logs are one JSON object per line on stderr with the record's own level
  (`EVILFLOWERS_LOG_FORMAT=json`): Celery's handler and its stdout/stderr proxy are off
  (`studio/workers/app.py`), and `PIL` logs WARNING and above only.
* The portal decodes at most `STUDIO_RENDER_CONCURRENCY` (default 2) full-size images at
  once; thumbnails of cleaned pages are cut from the 1600 px WebP preview (#12).

Service `portal` (ClusterIP :80) is VPN-only: no HTTPRoute.
URL: `http://portal.digitalizer.stu.k8s.internal`.

## Render (never apply by hand)

```bash
kubectl kustomize --load-restrictor=LoadRestrictionsNone deploy/k8s/overlays/dev > /tmp/digitalizer-dev.yaml
```

The flag is needed because the ConfigMap is generated from `configs/studio.k8s.toml`,
which lives outside this directory. The Ansible role drops the rendered file into
`/var/lib/rancher/k3s/server/manifests/` (Cookbooks/Deploy-a-Service §2).

## Prerequisites

The infrastructure side is tracked in the knowledge repository (knowledge#66, #67):

1. **NFS on gateway**: `raw-scans` (ro) and `digitized` (rw). The server address is set
   once, in `base/nfs-server.yaml`. Both are mounted under the **storage root**, which is
   also set once: `STUDIO_STORAGE_ROOT` in the overlay's `studio-env` (dev: `/data`).
   `components/storage-root` moves the mounts to `<root>/raw-scans` and `<root>/digitized`,
   and the app derives its `{storage_root}` paths from the same variable.
   Service jobs carry paths relative to the root. A service may therefore mount the
   storage at a different absolute path (its own `STORAGE_PATH`), as long as the layout
   below the root is the same.
2. **Workload label** `elvira.stuba.sk/workload=apps` on the nodes.
3. **Postgres, Valkey and Redis** in namespace `shared`, with:
   * the `digitalizer` database and role;
   * Valkey (`valkey.shared.svc.cluster.local:6379`) db 8 as the broker (#8). The
     `STUDIO_REDIS_URL` secret and both KEDA triggers must name it; KEDA on any other
     host watches an empty db 8 and never scales, without an error;
   * read access to gateway's Redis db 7 (`redis.shared`, the catalog's queues, for
     backpressure). It stays there until the dev catalog moves.
4. **KEDA** and the PriorityClass `digitalizer-batch` (cluster-scoped; this base ships it,
   unless the platform owns it).
5. **Secrets** in namespace `digitalizer`: `ghcr-pull` and `studio-secrets`
   (`base/secret.example.yaml`).
6. **Shared vlm**: `http://vlm.shared.svc.cluster.local` (evilflowers-vlm-service).

**Publication is optional.** A project without a `[publish]` target never publishes: an
approved book is complete, with its files (PDF, PDF/A-2b, EPUB, IA item) on the digitized
export, and the portal counts it as done.

With a target, publishing uploads the access PDF to the catalog. The upload starts the
**catalog's own pipeline** on that PDF: analyzer, then text, image and equation extraction,
then embeddings. It runs on the catalog's workers (Redis db 7) and counts against the
backpressure that holds the digitalizer's bulk work.

## Verify (VPN on)

```bash
STUDIO_BASE_URL=http://portal.digitalizer.stu.k8s.internal deploy/dev/smoke.sh
curl -s http://portal.digitalizer.stu.k8s.internal/api/v1/scheduler
```
