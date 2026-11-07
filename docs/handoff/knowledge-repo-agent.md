# Handoff: Restoration Studio infrastructure in the knowledge repo

For: the agent that works in `github.com/EvilFlowersCatalog/knowledge` (Ansible + Obsidian vault).
From: the evilflowers-books-digitalizer session, 2026-11-07.
Status of everything below: **Proposal**. Nothing here is applied. Do not apply anything
without the user running it (vault rule: "Nothing auto-applies — you run it",
[Vault-Structure](http://evilflowers-knowledge.intranet.stu.k8s.internal/Vault-Structure.html)).

Knowledge-base URLs below were returned by the `evilflowers-knowledge` MCP on 2026-11-07.
Repo file paths were read through the GitHub MCP at commit `e6ce1bc` of `master`. Re-read
each file before you edit it; the vault changes daily.

---

## 1. Goal and scope

The books digitalizer is becoming a **Restoration Studio**: a long-running web app (FastAPI +
React SPA) with Celery workers. Operators review flagged pages over the VPN and publish books
to the catalog. It needs:

- the 10 TB scan disk (`vdc`, xfs, `/mnt/digital-library`, attached only to gateway) on the
  k3s workload node (control), over **NFS**;
- a database on the shared `postgresql18` and a Celery db on the shared Redis;
- a namespace `digitalizer` with its Secrets;
- an nginx location that serves published files in place (`external_url`);
- telemetry, CI redeploy, and vault documentation (a Proposal note plus four diagrams).

The app-side manifests are already written in the app repo (`deploy/k8s/`, kustomize). Your
job is the **host and cluster prerequisites, the Ansible role that delivers the manifests, and
the vault docs**. You write no application code.

### Live vs proposed (verified against the vault, 2026-11-07)

| Item | State | Evidence |
|---|---|---|
| Workload label `elvira.stuba.sk/workload=apps` on control | **live** | `ansible/inventory/host_vars/control.yml` `k8s_node_labels`; `group_vars/all/k3s.yml` `k8s_workload_node_selector`; [Dev-Environment-K8s](http://evilflowers-knowledge.intranet.stu.k8s.internal/Proposals/Dev-Environment-K8s.html) "Applied 2026-11-07" |
| k8s-bridge (nginx stream on 192.168.122.227, :5432/:6379) and the selector-less Services `postgres`, `redis` in namespace `shared` | **live** | `group_vars/all/k8s_bridge.yml` (`k8s_bridge_services`), `roles/legacy_bridge`, `roles/k8s_external_services`; same Dev-Environment-K8s note (D3, D4) |
| Bridge accepts pod traffic | **live** | `k8s_bridge_allow_from` = every k3s node IP + `cluster_cidr` (10.42.0.0/16) |
| VPN DNS `<svc>.<ns>.stu.k8s.internal` | **live** for every namespace | [Deploy-a-Service](http://evilflowers-knowledge.intranet.stu.k8s.internal/Cookbooks/Deploy-a-Service.html) "What you get"; `group_vars/all/k3s.yml` `k8s_dns_zone` |
| Existing batch digitalizer (`roles/digitalizer`, Jobs on gateway with hostPath, namespace `evilflowers-dev`) | written, **not applied** | [Digitalizer](http://evilflowers-knowledge.intranet.stu.k8s.internal/Cookbooks/Digitalizer.html), [STATUS](http://evilflowers-knowledge.intranet.stu.k8s.internal/STATUS.html) |
| NFS server on gateway | **does not exist** | `find_port(2049)` returns nothing (no diagram, note, Ansible line or observed socket) |
| `nfs-common` on control | **missing** | `host_vars/control.yml` `storage_packages: []` |
| DB/role `digitalizer` on postgresql18 | **missing** | no Ansible task creates app databases; only `roles/postgresql/tasks/monitoring_role.yml` creates a role |
| Redis db 8 | **unverified** | dev uses db 1 and Celery db 7 (`group_vars/all/evilflowers.yml`); [Storage](http://evilflowers-knowledge.intranet.stu.k8s.internal/Infrastructure/Storage.html) lists "celery brokers db 2 / 7" |
| nginx `location /digitized/` | **missing** | `ansible/roles/nginx/files/sites/elvira.stuba.sk` is static-only |
| Namespace `digitalizer`, Secrets `ghcr-pull`, `studio-secrets` | **missing** | — |
| `vlm` Deployment (PaddleOCR-VL, CPU, HTTP :8000) and an ONNX layout model in the worker image | **in progress in the app repo, to be confirmed** | app repo `deploy/k8s/base/vlm.yaml` (placeholder image, replicas 0) |

The app repo's `deploy/k8s/README.md` still says the workload label and the bridge are
"written, not applied". That is stale. Treat them as prerequisites already met.

---

## 2. Facts you build on (do not re-derive, but re-check)

| Fact | Value | Source |
|---|---|---|
| gateway private IP | 192.168.122.227 (`enp1s0`) | `host_vars/gateway.yml` `k3s_node_ip`; [gateway entity](http://evilflowers-knowledge.intranet.stu.k8s.internal/Entities/gateway.html) |
| control private IP | 192.168.122.60 | `host_vars/control.yml` `k3s_node_ip`; [control entity](http://evilflowers-knowledge.intranet.stu.k8s.internal/Entities/control.html) |
| libvirt net | 192.168.122.0/24, also holds the hypervisor 192.168.122.1 (NFS server for `/mnt/shared`) | `host_vars/gateway.yml` `storage_mounts`, `firewall_trusted_interfaces` comment |
| Pod CIDR / Service CIDR | 10.42.0.0/16 / 10.43.0.0/16 | `group_vars/all/k3s.yml` |
| Scan disk | `/dev/vdc` → `/mnt/digital-library`, xfs, 10T, ~62 % used | `host_vars/gateway.yml`; [Storage](http://evilflowers-knowledge.intranet.stu.k8s.internal/Infrastructure/Storage.html); live disk stats in the gateway entity |
| Scans owner | `elvira:elvira` = **1001:1001**, mode **0770** (read 2026-11-06) | `host_vars/gateway.yml` `digitalizer_uid` / `digitalizer_gid` |
| gateway ufw | active; default deny in; public NIC `enp8s0` allows 22/80/443 only; **everything on `enp1s0` is allowed** (`firewall_trusted_interfaces`) | `host_vars/gateway.yml` firewall section |
| rpcbind :111 on 0.0.0.0, denied on the public NIC | `hardening_ufw_public_deny` | `host_vars/gateway.yml` (Known-Issues 11) |
| control ufw | **off** (Known-Issues 6g); do not switch it on from this work | `host_vars/control.yml` |
| postgresql18 | container `evilflowers-postgresql-postgresql18-1`, 172.28.1.1, PG 18.6, managed by `roles/postgresql` | `roles/postgresql/defaults/main.yml`; [postgresql18 entity](http://evilflowers-knowledge.intranet.stu.k8s.internal/Entities/postgresql18.html) |
| Postgres sees bridged clients as 172.28.0.1, already accepted by pg_hba | — | `group_vars/all/k8s_bridge.yml` header comment |
| Exporter connection limit | `postgresql_monitor_connection_limit: 20`, 13 databases on 2026-11-06 | `roles/postgresql/defaults/main.yml` |
| Redis | `elvira-core` redis:7, 172.28.1.2:6379, **no auth** | `host_vars/gateway.yml` `obs_redis_exporter_addr` |
| 1Password naming | `op://ELVIRA/<client>.<scope>.<component>/<field>`; refs in `op_refs` | `group_vars/all/onepassword.yml`; [ADR-0007](http://evilflowers-knowledge.intranet.stu.k8s.internal/Decisions/ADR-0007-secrets-in-1password.html), [Manage-Secrets](http://evilflowers-knowledge.intranet.stu.k8s.internal/Cookbooks/Manage-Secrets.html) |
| GHCR pull | `shared.infra.ghcr` (`username`, `read_token`) → Secret `ghcr-pull` | `onepassword.yml`; Deploy-a-Service §1 |
| Catalog API key (dev) | `stu.dev.digitalizer/api_key` (`op_refs.digitalizer_dev_api_key`) | `onepassword.yml`; Digitalizer §2 |
| Role template for k8s apps | `roles/intranet` (preflight → namespace + pull Secret → manifest into `/var/lib/rancher/k3s/server/manifests/` → wait) | Deploy-a-Service §2; `roles/intranet/tasks/main.yml` |
| CI redeploy namespaces | `github_runner_deploy_namespaces: [intranet, evilflowers-dev]` | `roles/github_runner/defaults/main.yml`; [CI-Runner](http://evilflowers-knowledge.intranet.stu.k8s.internal/Cookbooks/CI-Runner.html) |
| OTLP receiver | alloy-node on the pod's node, `http://$(HOST_IP):4318` | Deploy-a-Service §3 |
| Diagrams | `vault/evilflowers-knowledge/Diagrams/<slug>.puml` + rendered `.svg` and `.png`; render with `tools/diagrams/render.py` | [Vault-Structure](http://evilflowers-knowledge.intranet.stu.k8s.internal/Vault-Structure.html); `list_diagrams` |

### Capacity on control (Mimir, queried 2026-11-07 ~12:20 UTC)

| | CPU | Memory |
|---|---|---|
| Allocatable (`kube_node_status_allocatable`) | 6 | 32.57 GB (30.3 GiB) |
| Requests of running pods now | 2.56 | 13.92 GB (12.97 GiB) |
| Studio requests, vlm off (app repo `deploy/k8s`) | 2.3 | 6.25 GiB |
| **Total, vlm off** | **4.86 (81 %)** | **19.2 GiB (63 %)** |
| vlm on (+1 CPU / 4 GiB, to be confirmed) | 5.86 (98 %) | 23.2 GiB (77 %) |
| Studio limits, vlm off / on | 8 / 12 | 18 / 28 GiB |

With vlm on, a CI job (runner request 250m, `github_runner_max: 2`) or a rollout surge may not
fit. The Proposal note lists this as a risk. `/var/lib/rancher` is 200 GB, 28 % used (control
entity, live). The studio asks 80 Gi of local-path scratch there.

---

## 3. Changes per file (knowledge repo)

Paths are relative to the knowledge repo root. Follow the repo's own conventions: a comment
header per variable block, `--check --diff` support, secrets only from 1Password over stdin,
`no_log: true` on any task that handles a secret. Read [Run-Ansible](http://evilflowers-knowledge.intranet.stu.k8s.internal/Cookbooks/Run-Ansible.html) first.

### 3.1 NFS server on gateway (new role `nfs_server`)

Nothing in the repo serves NFS today. The hypervisor's `/mnt/shared` export is a different
machine. Create a small role instead of growing `roles/storage`, because `storage` runs on
every host from `playbooks/base.yml`.

`ansible/roles/nfs_server/defaults/main.yml`:
```yaml
---
# Kernel NFS server — ADDITION (vault: Proposals/Restoration-Studio). gateway exports two
# subtrees of the 10 TB scan disk (vdc) to the k3s workload nodes over the private network.
# Clients are node IPs, not the /24: the hypervisor (192.168.122.1) sits on that subnet too.
# kubelet mounts NFS from the host network, so pod IPs never reach nfsd.
nfs_server_packages: [nfs-kernel-server]
nfs_server_v4_only: true            # /etc/nfs.conf.d: vers3=n → no mountd/rpcbind needed by clients
nfs_server_clients: >-
  {{ (groups['k3s_server'] + groups['k3s_agent'])
     | difference([inventory_hostname])
     | map('extract', hostvars, 'k3s_node_ip') | list }}
nfs_server_exports: []              # set in host_vars
```

`ansible/inventory/host_vars/gateway.yml`, new block after `digitalizer`:
```yaml
# ------------------------------------------------------------- nfs server ---
# playbooks/nfs-server.yml (vault: Proposals/Restoration-Studio). all_squash maps every
# client uid (the studio pods run as 10001) to the scans owner, so root on a node never
# becomes root on the disk. raw-scans is read-only ON THE SERVER: the scans are irreplaceable.
nfs_server_exports:
  - { path: /mnt/digital-library/raw-scans, options: "ro,sync,no_subtree_check,all_squash,anonuid={{ digitalizer_uid }},anongid={{ digitalizer_gid }},sec=sys" }
  - { path: /mnt/digital-library/digitized, options: "rw,sync,no_subtree_check,all_squash,anonuid={{ digitalizer_uid }},anongid={{ digitalizer_gid }},sec=sys" }
```

`ansible/roles/nfs_server/templates/elvira.exports.j2` → `/etc/exports.d/elvira.exports`:
```
# Managed by Ansible (roles/nfs_server). Do not edit.
{% for e in nfs_server_exports %}
{{ e.path }} {% for c in nfs_server_clients %}{{ c }}({{ e.options }}) {% endfor %}

{% endfor %}
```
Rendered today (clients = control only):
```
/mnt/digital-library/raw-scans 192.168.122.60(ro,sync,no_subtree_check,all_squash,anonuid=1001,anongid=1001,sec=sys)
/mnt/digital-library/digitized 192.168.122.60(rw,sync,no_subtree_check,all_squash,anonuid=1001,anongid=1001,sec=sys)
```
An app VM that joins with `k3s_node_ip` is added automatically, like the bridge allow-list.

`ansible/roles/nfs_server/tasks/main.yml`, in order:
1. assert `nfs_server_exports` paths exist (`stat`) and `nfs_server_clients | length > 0`;
2. `apt: nfs-kernel-server`;
3. when `nfs_server_v4_only`: template `/etc/nfs.conf.d/elvira.conf` with
   ```
   [nfsd]
   vers3=n
   ```
   (optionally `host=192.168.122.227` so nfsd binds the private address only; check that the
   installed nfs-utils supports it before you add it);
4. template the exports file (mode 0644) → handler `exportfs -ra`;
5. `systemd: name=nfs-server enabled=true state=started`.

`ansible/playbooks/nfs-server.yml`: one play, `hosts: gateway`, `become: true`, role
`nfs_server`. Do not add it to `site.yml` until the user decides (see Open questions).

**Directories** (on gateway, via the studio role's gateway play, as `roles/digitalizer/tasks/host.yml` does for `digitized/dev`):
`/mnt/digital-library/digitized` and `/mnt/digital-library/digitized/studio`, owner
`{{ digitalizer_uid }}:{{ digitalizer_gid }}`, mode `0770`, `state: directory` (never
recursive: never touch existing files).

### 3.2 Firewall

- **gateway, private NIC:** no rule needed. `enp1s0` is in `firewall_trusted_interfaces`
  (`host_vars/gateway.yml`), so control already reaches :2049. The README of the app repo
  suggests `ufw allow from 192.168.122.0/24 to any port 2049`; do **not** add it (redundant,
  and wider than the export list).
- **gateway, public NIC:** default deny already blocks :2049. Make it explicit like rpcbind,
  in `host_vars/gateway.yml`:
  ```yaml
  hardening_ufw_public_deny:
    - { port: 111,  proto: tcp, comment: "rpcbind not public" }
    - { port: 111,  proto: udp, comment: "rpcbind not public" }
    - { port: 2049, proto: tcp, comment: "nfsd not public" }
  ```
  Applied by `playbooks/hardening.yml`. The `ansible()` map shows that play with no roles, so
  it may use tasks/imports. Read the playbook and confirm how it consumes
  `hardening_ufw_public_deny` before you rely on this.
- **control:** no change. Its ufw is off (Known-Issues 6g) and must not be switched on as part of this work.

### 3.3 NFS client on control

`ansible/inventory/host_vars/control.yml`: `storage_packages: [nfs-common]` (today `[]`).
Applied by `playbooks/base.yml` (roles `base`, `storage`).

**Warning:** the same `storage` role also carries `storage_lv_grow` (`ubuntu-vg/ubuntu-lv`
→ 36g) for control. Run `ansible-playbook playbooks/base.yml --limit control --check --diff`
first. If the LV grow shows as a pending change, stop and ask the user. Do not let this
change grow the LV as a side effect.

### 3.4 PostgreSQL: database and role `digitalizer`

No task in the repo creates application databases. Mirror
`roles/postgresql/tasks/monitoring_role.yml` exactly: `docker exec -i
{{ postgresql_project }}-{{ postgresql_service }}-1` psql over **stdin**, the password from
`op_read` into a psql variable, `no_log: true`, create only when missing or with
`-e secrets_push=true`, and a check-mode debug message.

- `ansible/roles/postgresql/defaults/main.yml`: add `postgresql_app_databases: []` with a comment.
- `ansible/inventory/host_vars/gateway.yml` (postgresql block):
  ```yaml
  # Application databases on postgresql18 (roles/postgresql tasks/app_databases.yml).
  # Restoration studio (vault: Proposals/Restoration-Studio). Limits per Known-Issues 6e / R1
  # of Dev-Environment-K8s: one shared server for prod and dev.
  postgresql_app_databases:
    - name: digitalizer
      owner: digitalizer
      connection_limit: 20          # api + 3 Celery pools + beat + migrations, with headroom
      statement_timeout: 5min
      password_ref: "{{ op_refs.digitalizer_postgres_password }}"
  ```
- `ansible/inventory/group_vars/all/onepassword.yml`, under `op_refs`:
  ```yaml
  # Restoration studio → postgresql18 role `digitalizer` (Proposals/Restoration-Studio).
  digitalizer_postgres_password: "op://{{ op_vault }}/{{ op_item_prefix }}.digitalizer.postgres/password"
  ```
  The item `stu.digitalizer.postgres` (field `password`) must be created by the user. Give them
  the command, never a value:
  `op item create --vault ELVIRA --category Database --title stu.digitalizer.postgres --generate-password=32,letters,digits`
  (then check that the field is named `password`).
- `ansible/roles/postgresql/tasks/app_databases.yml`, SQL per entry (stdin, autocommit,
  `ON_ERROR_STOP=1`):
  ```sql
  \set pw '<op_read value, single quotes doubled>'
  SELECT 'CREATE ROLE digitalizer LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'digitalizer')\gexec
  ALTER ROLE digitalizer WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 20 PASSWORD :'pw';
  ALTER ROLE digitalizer SET statement_timeout = '5min';
  SELECT 'CREATE DATABASE digitalizer OWNER digitalizer' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'digitalizer')\gexec
  REVOKE ALL ON DATABASE digitalizer FROM PUBLIC;
  GRANT CONNECT, TEMP ON DATABASE digitalizer TO digitalizer;
  ```
  The schema is created by the app (Alembic, init container `evilflowers-digitalizer studio migrate`). No extensions are needed.
- Include it from `roles/postgresql/tasks/main.yml` (read that file first and place it after
  the server is up). Run it with `playbooks/postgresql.yml` (hosts gateway).
- Database count goes 13 → 14. That still fits `postgresql_monitor_connection_limit: 20`, and
  Alloy's autodiscovery picks the new database up without a change.

### 3.5 Redis: Celery db 8

There is no allocation registry. Before using db 8, verify that it is empty and that no client
config names it:
```
cd ansible
ansible gateway -b -a 'docker exec elvira-core-redis-1 redis-cli INFO keyspace'
```
(The container name is not confirmed. If that name fails, take it from `docker ps --format
'{{.Names}}' | grep redis` on gateway.) If db 8 is taken, pick the lowest free db ≥ 8 and tell the
user. The app config (`configs/studio.k8s.toml`) and the Secret must change together.
Record the allocation as a comment on the `redis` entry in
`group_vars/all/k8s_bridge.yml`, for example
`# dbs: dev 1 + Celery 7 (evilflowers.yml), studio Celery 8 (Proposals/Restoration-Studio)`,
and in the Proposal note. Never `FLUSHDB` (Dev-Environment-K8s R1).

### 3.6 The studio role (new, name to confirm: `roles/digitalizer_studio`, `playbooks/digitalizer-studio.yml`)

`roles/digitalizer` already exists for the batch Jobs (namespace `evilflowers-dev`). Do not
overload it. Copy `roles/intranet` (its defaults say "copy this role, rename the variables")
and add the gateway play:

`playbooks/digitalizer-studio.yml`:
1. play `hosts: gateway`: the directories of §3.1 (asserts `digitalizer_uid/gid` set);
2. play `hosts: k3s_server`: role `digitalizer_studio`.

`roles/digitalizer_studio/defaults/main.yml` (key values):
```yaml
studio_manifests_dir: /var/lib/rancher/k3s/server/manifests
studio_namespace: digitalizer
studio_image: ghcr.io/evilflowerscatalog/evilflowers-books-digitalizer
studio_image_tag: master
studio_pull_secret: ghcr-pull
studio_node_selector: "{{ k8s_workload_node_selector }}"      # elvira.stuba.sk/workload: apps
studio_nfs_server: "{{ hostvars['gateway'].k3s_node_ip }}"     # replaces the literal 192.168.122.227 in storage.yaml
studio_environment: development                                # OTel deployment.environment.name
studio_redis_db: 8                                             # §3.5
studio_database_url_template: "postgresql+psycopg://digitalizer:{pw}@postgres.shared.svc.cluster.local:5432/digitalizer"
studio_api_key_ref: "{{ op_refs.digitalizer_dev_api_key }}"   # or a new item, see Open questions
studio_hostname: "studio.{{ studio_namespace }}.{{ k8s_dns_zone }}"   # = studio.digitalizer.stu.k8s.internal
```

`roles/digitalizer_studio/tasks/main.yml`, same order as `roles/intranet/tasks/main.yml`:
1. preflight: a node matches `studio_node_selector`; `exportfs -v` on gateway
   (`delegate_to: gateway`, `changed_when: false`, `check_mode: false`) lists both exported
   paths, or the role stops with the hint "run playbooks/nfs-server.yml first";
2. namespace `digitalizer`; Secret `ghcr-pull` from `op_refs.ghcr_pull_*` (copy verbatim);
3. Secret `studio-secrets` (keys `database-url`, `redis-url`, `evilflowers-api-key`), built
   from 1Password over stdin, only when missing or `secrets_push`, `no_log: true`:
   - `database-url` = template above with `op_read op_refs.digitalizer_postgres_password` (URL-encode the password),
   - `redis-url` = `redis://redis.shared.svc.cluster.local:6379/{{ studio_redis_db }}`,
   - `evilflowers-api-key` = `op_read studio_api_key_ref`;
4. manifest `{{ studio_manifests_dir }}/digitalizer-studio.yaml` (owner root, 0600);
5. wait: Deployments `studio-api`, `worker-cpu`, `worker-ai`, `worker-io` exist → restart on
   new pull Secret or `ProgressDeadlineExceeded` → `rollout status --timeout=600s` (image is
   large, model load is slow). Skip `vlm` (replicas 0).

**How the manifest gets into the role (open question for the user, recommend A):**
- **A (recommended):** render the app repo at a pinned commit
  (`kubectl kustomize --load-restrictor=LoadRestrictionsNone deploy/k8s/overlays/dev`), then
  commit it as `roles/digitalizer_studio/templates/digitalizer-studio.yaml.j2`. Replace only
  the NFS server IP, the image tag, the node selector and the environment label with the
  variables above. Note the source commit in the header. The configMapGenerator hash suffix
  stays as rendered. Re-render when the app's `deploy/k8s` or `configs/studio.k8s.toml` changes.
- B: the role runs kustomize at apply time from a checkout of the app repo. That adds a
  network and git dependency to every run, so the repo has no precedent for it.

The manifest must not contain `studio-secrets` (it is created in step 3, never written to disk).

Labels: the app's base adds `app.kubernetes.io/part-of: elvira` and `app.kubernetes.io/name`
per Deployment (`studio-api`, `studio-worker-cpu`, `studio-worker-ai`, `studio-worker-io`,
`studio-vlm`). Add `app.kubernetes.io/managed-by: ansible` in the template (Deploy-a-Service §2).

### 3.7 nginx: serve published files in place

The app links files as `https://elvira.stuba.sk/digitized/<project>/<item_id>/<file>`
(`files_base_url` in the app's `configs/studio.k8s.toml`). In
`ansible/roles/nginx/files/sites/elvira.stuba.sk`, inside the `listen 443` server block, add:
```nginx
	# Restoration studio: published files, served in place from the scan disk
	# (vault: Proposals/Restoration-Studio). Read-only; no listing.
	location /digitized/ {
		alias /mnt/digital-library/digitized/studio/;
		autoindex off;
		add_header Cache-Control "public, max-age=3600";
		types { application/pdf pdf; application/epub+zip epub; image/jpeg jpg; text/plain txt; }
	}
```
**Blocking issue:** `/mnt/digital-library` is `elvira:elvira 0770` and the scan dir "is not
world-readable" ([Storage](http://evilflowers-knowledge.intranet.stu.k8s.internal/Infrastructure/Storage.html)).
nginx's worker user cannot traverse it. Options for the user (see Open questions). Do not
change modes or groups on the scan disk without an explicit answer. Apply with
`playbooks/gateway.yml --tags nginx` (check the tag name in `roles/nginx`) after
`--check --diff`, and run `nginx -t` first.

### 3.8 DNS, ingress, auth

Nothing to add. The SPA is VPN-only, without login (user decision, like flower and Grafana in
Dev-Environment-K8s D7). The name `http://studio.digitalizer.stu.k8s.internal` comes from the
CoreDNS zone and the `k8s-stu` router (Deploy-a-Service "What you get"). No HTTPRoute, no
public vhost.

### 3.9 CI redeploy

- `ansible/roles/github_runner/defaults/main.yml`:
  `github_runner_deploy_namespaces: [intranet, evilflowers-dev, digitalizer]`, then
  `playbooks/github-runner.yml --check --diff` → apply (Deploy-a-Service §4).
- The user adds `evilflowers-books-digitalizer` to the runner group `elvira-k3s`'s selected
  repositories in GitHub ([CI-Runner](http://evilflowers-knowledge.intranet.stu.k8s.internal/Cookbooks/CI-Runner.html) §1).
- The app's `deploy` job is the app session's work, not yours.

### 3.10 Telemetry (Deploy-a-Service §3)

The app manifests already set `HOST_IP`, `NODE_NAME`, `POD_*`, `OTEL_SERVICE_NAME` from
`app.kubernetes.io/name`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://$(HOST_IP):4318`,
`http/protobuf`, and `OTEL_RESOURCE_ATTRIBUTES` with `deployment.environment.name`. Logs are
JSON (`EVILFLOWERS_LOG_FORMAT=json`). Nothing changes in `roles/observability`.
Report back (do not fix, it is the app repo): the pod template lacks
`elvira.stuba.sk/instrumentation: otel-sdk`. If the app exports spans with the SDK, Beyla
traces every request a second time.

---

## 4. Diagrams

The four drafts are in the app repo, `docs/design/`. They already use the vault include
(`!include ../../../tools/diagrams/elvira.iuml`), the header (What/Kind/Source/Render), the
footer and `$legend_live_proposed()`. All four were checked with `check_diagram(source=…)` on
2026-11-07 and **pass** (lint: none; facts: 0 warn, info only: "new entity" for the studio
components, which is expected). The drafts already include the vlm service and the ONNX layout
model, marked "(TBC)".

| Draft (app repo) | Vault target | check_diagram |
|---|---|---|
| `docs/design/deployment-restoration-studio.puml` | `vault/evilflowers-knowledge/Diagrams/deployment-restoration-studio.puml` | pass (0 warn, 10 info, 4 ok) |
| `docs/design/component-restoration-studio.puml` | `vault/evilflowers-knowledge/Diagrams/component-restoration-studio.puml` | pass (lint clean; facts 0 warn) |
| `docs/design/sequence-restoration-ingest.puml` | `vault/evilflowers-knowledge/Diagrams/sequence-restoration-ingest.puml` | pass (0 warn, 9 info) |
| `docs/design/sequence-restoration-review.puml` | `vault/evilflowers-knowledge/Diagrams/sequence-restoration-review.puml` | pass (0 warn, 5 info, 1 ok) |

`component-` is a new slug prefix: the vault has only `deployment-`, `network-` and `sequence-`
so far. The lint accepts it (kind `component`). Keep it unless the user objects.

Steps:
1. Ask the user to copy the four files verbatim from the app repo, or take them from the same
   commit as the role's manifest. Do not redraw them.
2. Run `check_diagram(name="<slug>", source=<file text>)` on each. The verdict must stay `pass`.
   If you changed a fact in §3 (for example the Redis db), update the label in
   `deployment-restoration-studio` and `sequence-restoration-ingest` first.
3. Render: `python3 tools/diagrams/render.py vault/evilflowers-knowledge/Diagrams/<slug>.puml`
   for each, then check that the `.svg` and `.png` exist next to the `.puml`.
4. After the commit: run `check_diagram(name="<slug>")`. It must report the svg as `current`,
   and the width as `pass` or `marginal`.
5. Optional: run `consistency()` and check that it reports nothing new for these diagrams.

---

## 5. Proposal note (paste as `vault/evilflowers-knowledge/Proposals/Restoration-Studio.md`)

```markdown
---
tags: [proposal, k8s, digitalizer, storage]
status: proposed
date: 2026-11-07
updated: 2026-11-07
aliases: [Studio, Digitalizer-Studio]
---

# Proposal: Restoration Studio on k3s (NFS scans, shared Postgres/Redis, VPN-only)

> **Proposed, not applied.** Nothing here is live. The batch digitalizer
> ([[Digitalizer]], Jobs on gateway) stays as written; the studio is a separate
> long-running app in namespace `digitalizer` on the workload node(s).

![[deployment-restoration-studio.svg]]

Source: `Diagrams/deployment-restoration-studio.puml`. Design: `Diagrams/component-restoration-studio.puml`.
Flows: `Diagrams/sequence-restoration-ingest.puml`, `Diagrams/sequence-restoration-review.puml`.
App: [[Books-Digitalizer]] (repo `evilflowers-books-digitalizer`, `deploy/k8s/`, `configs/studio.k8s.toml`).

## What it is
The digitalizer as a restoration studio: a FastAPI API (REST, SSE, IIIF tiles, the React SPA)
and Celery pools (`interactive`+`cpu`, `ai`, `io`+beat) over one image
`ghcr.io/evilflowerscatalog/evilflowers-books-digitalizer`. Books are processed automatically;
only flagged books wait for an operator, who fixes regions in the page studio, re-runs single
pages on a priority queue and approves. Published books are linked in place
(`external_url`), not uploaded.

## Decisions

| # | Decision | Why |
|---|---|---|
| S1 | **Scans over NFS from gateway** (`vdc`, two exports: `raw-scans` ro, `digitized` rw, `all_squash` → elvira 1001:1001, clients = k3s workload node IPs) | Heavy work leaves gateway (production) and runs on control / app VMs. `ro` is enforced by the server: the scans are irreplaceable. |
| S2 | **Namespace `digitalizer`**, `nodeSelector elvira.stuba.sk/workload=apps` | Same placement contract as dev ([[Dev-Environment-K8s]] D5). |
| S3 | **Shared servers**: database `digitalizer` on `postgresql18`, Celery db 8 on the core Redis, both via `postgres`/`redis` in namespace `shared` (k8s-bridge) | No new server; same path as dev (D1, D3, D4). |
| S4 | **VPN-only, no login**: `http://studio.digitalizer.stu.k8s.internal` | Like flower and Grafana (D7). |
| S5 | **Publish in place**: catalog acquisitions `external_url` → `https://elvira.stuba.sk/digitized/…`, host nginx `alias /mnt/digital-library/digitized/studio/` | No copy of a multi-TB corpus into catalog storage ([[Digitalizer]] §8). |
| S6 | **CPU-only AI**: layout model (ONNX) inside the worker image; PaddleOCR-VL as a separate `vlm` Deployment, replicas 0, scaled up on demand | No GPU on either VM. **To be confirmed** (in progress in the app repo). |
| S7 | Role `digitalizer_studio` delivers a rendered manifest into the k3s manifests dir; Secrets from 1Password over stdin | [[Deploy-a-Service]] §2. |

## Resources

| Workload | Replicas | Requests → limits |
|---|---|---|
| studio-api (+ init `migrate`) | 1, Recreate | 100m / 512Mi → 1 / 2Gi |
| worker-cpu (`interactive,cpu`, concurrency 3) | 1 | 1.5 / 3Gi → 4 / 8Gi |
| worker-ai (`ai`, concurrency 1) | 1 | 0.5 / 2Gi → 2 / 6Gi |
| worker-io (`io` + beat) | 1, Recreate | 0.2 / 768Mi → 1 / 2Gi |
| vlm (PaddleOCR-VL, TBC) | **0**, on demand | 1 / 4Gi → 4 / 10Gi |

control (Mimir 2026-11-07): allocatable 6 CPU / 30.3 GiB, requested 2.56 CPU / 12.97 GiB.
With the studio: **4.86 CPU / 19.2 GiB** (vlm off), **5.86 CPU / 23.2 GiB** (vlm on).

Storage: PVC `scratch` 80Gi local-path on control `/var/lib/rancher` (200 GB, 28 % used);
static NFS PVs `digitalizer-raw-scans` (ROX) and `digitalizer-digitized` (RWX, Retain);
outputs under `/mnt/digital-library/digitized/studio/<project>/<item_id>/` on `vdc`
(10T, ~62 % used).

## Rollout
1. 1Password: `stu.digitalizer.postgres` (`password`). The API key is decided (reuse `stu.dev.digitalizer` or a new item).
2. control: `nfs-common` (`host_vars/control.yml` `storage_packages`, `playbooks/base.yml --limit control`).
3. gateway: `playbooks/nfs-server.yml` (exports), `playbooks/hardening.yml` (2049 public deny).
4. gateway: `playbooks/postgresql.yml` (role + database `digitalizer`); Redis db 8 verified empty.
5. `playbooks/digitalizer-studio.yml`: dirs on gateway, namespace, Secrets, manifest, rollout.
6. `playbooks/github-runner.yml` (namespace `digitalizer` in `github_runner_deploy_namespaces`).
7. nginx `location /digitized/` once read access for nginx is decided.
8. Smoke test over the VPN (`deploy/dev/smoke.sh` in the app repo); traces/logs in Grafana.

## Risks

| # | Risk | L / I | Mitigation |
|---|---|---|---|
| R1 | control CPU near full with vlm on (98 % requested); CI jobs may not schedule | M / M | vlm stays at 0 except for formula passes; app VMs |
| R2 | NFS `rw` export writable by any process on control | L / H | clients = node IPs only; `raw-scans` exported `ro`; `all_squash` (no root) |
| R3 | NFS hard mounts hang pods if gateway reboots | M / M | `hard` mounts recover; studio is not user-facing for readers |
| R4 | Published files public on `elvira.stuba.sk` | M / H | user decision on what may be public (open-access only) |
| R5 | Shared Postgres/Redis with prod | M / H | `CONNECTION LIMIT 20`, `statement_timeout`; own Redis db; never `FLUSHDB` |
| R6 | Studio and the batch Job both write under `digitized/` | L / M | separate subtrees: `studio/` vs `dev/`, `prod/` |
| R7 | Outputs on `vdc` have no backup ([[Backups]]) | M / H | decide before publishing to prod |
| R8 | Both exports are on the same filesystem (`vdc`); with `no_subtree_check` nfsd checks a file handle against the filesystem, not the subtree, so a client of the rw `digitized` export holding a raw-scans handle could write there as 1001 | L / H | user decision: `subtree_check` on `digitized`, accept (exports(5) "mild security implication"), or a separate filesystem for outputs |
```

Also update, each with one line (and bump `updated:`):
- `Home.md` → "Proposals:" list: `· [[Restoration-Studio]] (restoration studio on k3s, NFS scans)`.
- `STATUS.md` → "Proposed (designed, not implemented)": one bullet with the four diagram slugs
  and "written, not applied" once the role exists.
- `Applications/Books-Digitalizer.md` and `Cookbooks/Digitalizer.md` §8: link `[[Restoration-Studio]]`.
- `Infrastructure/Storage.md` (gateway `/mnt/digital-library` paragraph): "NFS export to the
  k3s workload nodes: proposed ([[Restoration-Studio]])."
- `Diary/2026-11-07.md` (or today's): a one-line entry, per [Vault-Structure](http://evilflowers-knowledge.intranet.stu.k8s.internal/Vault-Structure.html).

---

## 6. Verification (VPN on; the user runs every apply)

Every playbook first runs with `--check --diff`, which is the gate in
[Run-Ansible](http://evilflowers-knowledge.intranet.stu.k8s.internal/Cookbooks/Run-Ansible.html).
Then:
```
cd ansible
# NFS server
ansible gateway -b -a 'exportfs -v'                          # two paths, client 192.168.122.60, ro / rw, all_squash
ansible gateway -b -a 'ss -ltnp' | grep 2049                  # nfsd listening
ansible control -b -a 'dpkg -s nfs-common'
# mount test from control (then unmount)
ansible control -b -m shell -a 'mkdir -p /mnt/nfs-test && mount -t nfs4 -o vers=4.2,ro 192.168.122.227:/mnt/digital-library/raw-scans /mnt/nfs-test && ls /mnt/nfs-test | head -3; umount /mnt/nfs-test'
ansible control -b -m shell -a 'mount -t nfs4 -o vers=4.2 192.168.122.227:/mnt/digital-library/raw-scans /mnt/nfs-test && touch /mnt/nfs-test/x; echo rc=$?; umount /mnt/nfs-test'   # MUST fail: Read-only file system
# public NIC closed (from a machine off the VPN)
nc -vz -w3 elvira.stuba.sk 2049                                # MUST time out / be refused
# Postgres
ansible gateway -b -m shell -a "docker exec -i evilflowers-postgresql-postgresql18-1 sh -c 'PGPASSWORD=\"\$POSTGRES_PASSWORD\" psql -h 127.0.0.1 -U \"\${POSTGRES_USER:-postgres}\" -d postgres -qtAc \"SELECT datname, pg_get_userbyid(datdba) FROM pg_database WHERE datname=\$\$digitalizer\$\$; SELECT rolname, rolconnlimit FROM pg_roles WHERE rolname=\$\$digitalizer\$\$\"'"
# Redis db 8 before and after
ansible gateway -b -a 'docker exec <redis container> redis-cli INFO keyspace'
# Cluster
ansible control -b -a 'k3s kubectl -n digitalizer get pods,pvc,svc -o wide'
ansible control -b -a 'k3s kubectl get pv digitalizer-raw-scans digitalizer-digitized'
curl -s http://studio.digitalizer.stu.k8s.internal/healthz
```
The PVCs `raw-scans` and `digitized` must be `Bound`, `scratch` becomes `Bound` once the
first pod lands, and the `migrate` init container must exit 0.

Grafana (`http://grafana.observability.stu.k8s.internal`), as in Deploy-a-Service §6:
- Tempo `{resource.service.name="studio-api"}`. `telemetry.sdk.name` is either
  `opentelemetry` or `beyla`; seeing both for one pod means the `otel-sdk` label is missing.
- Loki `{namespace="digitalizer", service_name=~"studio-.*"} | json`.
- Mimir `kube_pod_container_resource_requests{namespace="digitalizer"}`. Check the totals
  against §2.

## 7. Rollback (reverse order; data is never deleted without the user's explicit say-so)

1. Studio: remove `/var/lib/rancher/k3s/server/manifests/digitalizer-studio.yaml` (the addon
   controller re-applies whatever is in that directory, so deleting objects alone does not
   stick). Then `k3s kubectl delete namespace digitalizer`. The PVs are `Retain`:
   `k3s kubectl delete pv digitalizer-raw-scans digitalizer-digitized` deletes nothing on the disk.
2. nginx: revert the `location /digitized/` hunk, then `nginx -t` and reload via the role.
3. CI: drop `digitalizer` from `github_runner_deploy_namespaces`, re-apply.
4. NFS: `exportfs -ua`, remove `/etc/exports.d/elvira.exports` and `/etc/nfs.conf.d/elvira.conf`,
   `systemctl disable --now nfs-server`. Leave the package installed unless the user asks.
   Revert the 2049 line in `hardening_ufw_public_deny`.
5. control: `nfs-common` can stay (harmless); revert `storage_packages` if the user wants.
6. Postgres: `ALTER ROLE digitalizer NOLOGIN` is the safe rollback. `DROP DATABASE digitalizer`
   and `DROP ROLE digitalizer` run **only** on the user's explicit request.
7. Redis: nothing to undo. Never `FLUSHDB`.
8. `digitized/studio/` on `vdc`: never deleted by rollback.

## 8. Open questions (only the user can answer)

1. **Role/playbook names**: `roles/digitalizer_studio` + `playbooks/digitalizer-studio.yml`,
   namespace `digitalizer`? Or should the studio replace `roles/digitalizer` (batch Jobs in
   `evilflowers-dev`, written but never applied)?
2. **Manifest delivery**: option A (rendered kustomize output committed as a template, pinned
   to an app commit) or B (kustomize at apply time)?
3. **nginx read access** to `/mnt/digital-library/digitized/studio` (the disk is `0770
   elvira:elvira`). Options: (a) a default POSIX ACL `u:www-data:rx` on `digitized/studio`
   only, plus `--x` on the parent directories; (b) add `www-data` to group `elvira`, which also
   gives write access to the scans because the group has `rwx`; (c) serve the files from the
   catalog instead.
4. **Public exposure**: may everything under `digitized/studio` be public on
   `https://elvira.stuba.sk/digitized/`? Only `open-access` items? Should archival PDF/A masters
   be excluded from the alias?
5. **Catalog target**: the app config publishes to the **dev** catalog. The knowledge base
   names no prod API URL or prod API key yet. When should prod come, and under which 1Password item?
6. **API key**: reuse `stu.dev.digitalizer/api_key` (batch Job) or create a separate key/item
   (e.g. `stu.dev.digitalizer-studio`) for audit?
7. **Redis db**: is db 8 free, and is there a registry the user keeps outside the repo? Which
   db does prod Celery use (Storage says "2 / 7")?
8. **NFS in `site.yml`**: should `playbooks/nfs-server.yml` join `site.yml`, or stay
   on-demand like `digitalizer.yml`?
9. **Backups**: outputs on `vdc` have no backup (vault note `[[Backups]]`; [Rebuild-Gateway](http://evilflowers-knowledge.intranet.stu.k8s.internal/Runbooks/Rebuild-Gateway.html) says backups "do not exist yet" for the Docker volumes and the Postgres volume). Is that acceptable before prod publishing?
10. **vlm**: final image name, RAM need and whether it runs on control at all (98 % CPU
    requested with it on). Should it wait for an app VM?
11. **Subtree isolation** (Proposal R8): both exports live on `vdc`. Use `subtree_check` on
    `digitized` (slower, sensitive to renames), accept `no_subtree_check`, or put the outputs on
    a separate filesystem?
12. **Output volume**: expected TB per faculty. About 3.8–4.2 TB are free on `vdc` (Storage
    note 4.2 TB on 2026-11-06; live 61.7 % used of 10T).
