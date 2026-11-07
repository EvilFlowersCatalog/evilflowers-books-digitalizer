# Restoration studio on k3s

Design: `docs/design/deployment-restoration-studio.puml`. One image
(`ghcr.io/evilflowerscatalog/evilflowers-books-digitalizer`) runs the API and every
worker; the command decides the role. The formula service has its own image
(`…-digitalizer-vlm`, Dockerfile target `vlm`).

| Deployment | Role | Replicas | Requests → limits |
|---|---|---|---|
| `studio-api` | uvicorn (REST, SSE, tiles, SPA) | 1 (Recreate) | 100m/512Mi → 1/2Gi |
| `worker-cpu` | Celery `interactive,cpu` (ScanTailor, OCR, render), concurrency 3 | 1 | 1.5/3Gi → 4/8Gi |
| `worker-ai` | Celery `ai` (layout ONNX, formulas → vlm over HTTP), concurrency 1 | 1 | 0.5/2Gi → 2/6Gi |
| `worker-io` | Celery `io` + beat (discover, tiles, publish, export, recover_runs) | 1 (Recreate) | 0.2/768Mi → 1/2Gi |
| `vlm` | PaddleOCR-VL-1.6 on CPU, HTTP :8000 (`/v1/formulas`, `/readyz`) | **0** (scale on demand) | 1/4Gi → 4/6Gi |

Every studio pod (api and workers) runs the init container `migrate` (`alembic upgrade
head`, `pod-defaults.yaml`), so a worker never starts against an older schema.

Sized for control (6 vCPU, 32 GB, shared with LGTM/TEI/CI; 2.56 CPU requested before
the studio): the studio adds 2.3 CPU (4.86 total, 81%) while vlm is off, and vlm's
1-CPU request makes 5.86 (98%). Hence vlm stays at 0 replicas and is scaled up for a
formula pass (`kubectl -n digitalizer scale deploy/vlm --replicas=1`, then back to 0).

Service `studio` (ClusterIP :80) is VPN-only, like flower: no HTTPRoute.
URL: `http://studio.digitalizer.stu.k8s.internal`.

## Render (never apply by hand)

```bash
kubectl kustomize --load-restrictor=LoadRestrictionsNone deploy/k8s/overlays/dev > /tmp/studio-dev.yaml
```

The flag is needed because the ConfigMap is generated from `configs/studio.k8s.toml`,
outside this directory. The Ansible role drops the rendered file into
`/var/lib/rancher/k3s/server/manifests/` (Cookbooks/Deploy-a-Service §2).

## Prerequisites

The infrastructure side (Ansible role, NFS server, database, secrets, nginx) is specified
in **`docs/handoff/knowledge-repo-agent.md`**; this list only says what the manifests
assume. Status as of 2026-11-07:

1. **NFS on gateway** — *to build* (handoff §3.1–3.3). Two exports, `raw-scans` (ro) and
   `digitized` (rw), `all_squash` to the scans owner; clients are the k3s **node IPs**
   (today only control, `192.168.122.60`), not the /24 (the hypervisor sits on it too).
   No ufw rule: gateway trusts its private NIC `enp1s0`. The server address is set once,
   in `base/nfs-server.yaml` (the value the Ansible role templates).
2. **Workload label** `elvira.stuba.sk/workload=apps` on control — *live*.
3. **Postgres/Redis**: the k8s-bridge and the Services `postgres`, `redis` in namespace
   `shared` — *live*. The `digitalizer` database/role and Redis db 8: handoff §3.4–3.5.
4. **Secrets** in namespace `digitalizer`: `ghcr-pull`, and `studio-secrets`
   (`base/secret.example.yaml`: `database-url`, `redis-url`, `evilflowers-api-key`),
   created by the role (handoff §3.6).
5. **Publishing uploads copies** to the catalog for now: `configs/studio.k8s.toml` sets no
   `files_base_url`, because the catalog API cannot create `external_url` acquisitions
   yet (a project that sets it fails fast). Publishing in place (gateway nginx serving
   `/digitized/` from the scan disk, handoff §3.7) waits for the catalog server change
   described in `docs/design/catalog-protocol-requests.md`.
6. **vlm image**: `docker build --target vlm -t ghcr.io/evilflowerscatalog/evilflowers-books-digitalizer-vlm:master .`
   and push it; the CI workflow builds only the studio image.

## Verify (VPN on)

```bash
STUDIO_BASE_URL=http://studio.digitalizer.stu.k8s.internal deploy/dev/smoke.sh
```
