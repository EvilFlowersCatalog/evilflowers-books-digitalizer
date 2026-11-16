# Deploy

The digitalizer is a portal (API + SPA) and an orchestrator (the job ledger). Every
digitalization job runs on a service worker on the shared broker:

| Stage | Service task |
| --- | --- |
| clean, erase | `evilflowers_scan.clean`, `evilflowers_scan.erase` (evilflowers-scan-service) |
| analyze | `evilflowers_ocr_worker.standalone.hocr` ∥ `evilflowers_analyzer.standalone.layout` |
| formulas | `evilflowers_equation.standalone.formulas` |
| render | `evilflowers_render.book` (evilflowers-render-service) |

The batch pipeline and its run-to-completion deployments (screen, systemd, Docker Compose,
the k8s Job) were retired in evilflowers-books-digitalizer#6.

* **k3s**: [`k8s/`](k8s/README.md). Two Deployments of one image, rendered with kustomize and
  applied by the Ansible role in the knowledge repository (knowledge#66).
* **Local development**: [`dev/`](dev/README.md). Postgres and Redis in Docker; run the API and
  the orchestrator from the checkout, and the service workers you need from their own
  repositories on the same Redis, with `STORAGE_PATH` set to the studio's storage root.

## Configuration and environment

| Variable | Meaning |
| --- | --- |
| `STUDIO_CONFIG` | the studio TOML (`configs/studio.k8s.toml` in the cluster) |
| `STUDIO_DATABASE_URL`, `STUDIO_REDIS_URL` | the database and the broker (secrets) |
| `STUDIO_STORAGE_ROOT` | where this pod mounts the shared storage (the services' `STORAGE_PATH`) |
| `DECIDER_URL`, `DECIDER_VISION_URL`, `DECIDER_TIMEOUT`, `DECIDER_ENABLED` | the decision services (#14, #15; knowledge#77) |
| `EVILFLOWERS_API_KEY` | catalog token for publication (optional) |
| `EVILFLOWERS_LOG_FORMAT`, `EVILFLOWERS_LOG_LEVEL` | `json` for one object per line; `INFO` by default |

The runtime contract (commands, probes, metrics, logs) is in
[docs/deployment.md](../docs/deployment.md).
