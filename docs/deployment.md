# Deployment (self-hosted Prefect on the VM)

The VM runs a self-hosted Prefect stack (server + UI + worker) in Docker. Flow
metadata stays on-prem; the worker image carries the full imaging toolchain.

## 1. Prerequisites

- The raw scans mounted at `/mnt/digital-library/raw-scans` (read-only is fine).
- Docker + Docker Compose.
- The metadata Excel at `configs/catalog.xlsx` (or adjust `[metadata].excel_path`).

## 2. Configure

```bash
cp deploy/.env.example deploy/.env       # set POSTGRES_PASSWORD, PREFECT_UI_API_URL,
                                         # RAW_SCANS_MOUNT, OUTPUT_DIR, CACHE_DIR
```

Review `configs/pipeline.toml` — `[source]`, `[metadata].columns`, `[cover]`,
and `[orchestration]` (set `max_parallel_books`/`ocr_jobs` to the VM's cores).

## 3. Start the server + worker

```bash
cd deploy
docker compose -f docker-compose.prefect.yml up -d postgres prefect-server
# build the toolchain image + start the worker (creates the work pool)
docker compose -f docker-compose.prefect.yml up -d --build worker
```

The UI is at `http://<vm>:4200`. Verify the worker is healthy under
**Work Pools → digitizer-pool**.

## 4. Register deployments

```bash
docker compose -f docker-compose.prefect.yml exec worker \
  python -m evilflowers_books_digitalizer serve      # applies deploy/prefect.yaml
```

## 5. Run

From the UI (**Deployments → Run**) or the CLI inside the worker:

```bash
# one faculty
docker compose ... exec worker prefect deployment run 'digitize-source/by-source' \
  -p source_key=svf
# whole corpus
docker compose ... exec worker prefect deployment run 'digitize-corpus/full-corpus'
```

For a quick local smoke test (no deployment, temporary server):

```bash
python -m evilflowers_books_digitalizer run-source svf --limit 3
```

## 6. Alerts

Configure a Prefect **Automation** (UI → Automations) so failures page you:

- **Trigger**: Flow run enters state `Failed` or `Crashed` (scope it to
  `digitize-source` / `digitize-corpus`).
- **Action**: send to a Slack webhook or email block.

A source flow is deliberately marked **Failed** only when at least
`[orchestration].fail_on_error_ratio` of its books error (a *systematic*
problem — missing binary, unreadable mount). A handful of bad books do not fail
the run; they are listed in the per-source summary artifact and the JSONL report
instead, so you don't get alert-spammed across an 880-book run.

## Scaling out

Add another worker (same or another VM) pointing `PREFECT_API_URL` at this
server and serving `digitizer-pool`. Because each book is idempotent
(skip-if-PDF-exists) and self-contained, multiple workers safely drain the same
corpus run.
