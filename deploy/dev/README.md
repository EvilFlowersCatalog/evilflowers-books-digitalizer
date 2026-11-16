# Restoration studio — run it locally

The portal and the orchestrator run on the host from the repo's venv; Postgres and Redis run
in Docker (`compose.yml`, ports 55432 / 56379 so they never clash with local servers).
Config: `configs/studio.dev.toml` (project `stu-local`, scans under `.data/raw-scans`).
All commands are written from the repository root.

## 1. Dev stack (Postgres 18 + Redis)

```bash
docker compose -f deploy/dev/compose.yml up -d
docker compose -f deploy/dev/compose.yml ps        # both "healthy"
```

## 2. Environment (every terminal below)

```bash
export STUDIO_CONFIG=$PWD/configs/studio.dev.toml
export STUDIO_DATABASE_URL=postgresql+psycopg://studio:studio@127.0.0.1:55432/studio
export STUDIO_REDIS_URL=redis://127.0.0.1:56379/0
mkdir -p .cache/studio            # beat's schedule file; celery does not create the dir
```

Schema (once, and after every new migration):

```bash
.venv/bin/evilflowers-digitalizer studio migrate     # alias of `studio db-upgrade`
```

## 3. Processes

One terminal each, or all at once with a Procfile runner (`honcho start -f deploy/dev/Procfile`,
`overmind start -f …`): copy the block below into `deploy/dev/Procfile`.

```Procfile
portal:       .venv/bin/uvicorn evilflowers_books_digitalizer.studio.api.app:create_app --factory --reload --host 127.0.0.1 --port 8000
orchestrator: .venv/bin/celery -A evilflowers_books_digitalizer.studio.workers.app:celery worker -Q digitalizer.portal -n orchestrator@%h --beat --schedule .cache/studio/celerybeat-schedule --concurrency 2 --loglevel INFO
spa:          pnpm -C frontend dev
```

The same roles as in k8s (`deploy/k8s/base/orchestrator.yaml`): the orchestrator owns the
portal queue and beat (pump every 15 s, reconcile every 60 s) and must run exactly once.

**The digitalization jobs run on the services** (evilflowers-books-digitalizer#6). A run
plans its jobs and then waits until their workers take them: start the ones you need from
their own repositories, on the same Redis (`redis://127.0.0.1:56379/0`), with `STORAGE_PATH`
set to this config's storage root (`$PWD/.data`), e.g. evilflowers-scan-service for clean.
The task names and queues are `studio.config.DEFAULT_ROUTES`. Without them, the portal's
own features (books, page studio on already cleaned pages, labelling, the scheduler) work;
a run stays `running` with its jobs `dispatched`.

Scheduling (`[scheduling]` in the config): `GET /api/v1/scheduler` shows the admission
mode and capacity; `POST /api/v1/scheduler/pause` / `resume` hold and release bulk work.

macOS: if a forked worker child crashes in an Objective-C initializer, start the
orchestrator with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` (a macOS fork-safety check, not
a bug in the task code).

SPA (first time): `pnpm -C frontend install`. `pnpm dev` serves http://127.0.0.1:5173 and
proxies `/api` to the API on :8000. Without the dev server, the API serves the last
`pnpm -C frontend build` from `frontend/dist` (`STUDIO_STATIC_DIR`) at http://127.0.0.1:8000.

## 4. Smoke test

```bash
deploy/dev/smoke.sh                                  # GET /api/v1/projects on :8000
STUDIO_BASE_URL=http://127.0.0.1:5173 deploy/dev/smoke.sh   # through the Vite proxy
```

Then import the synthetic tree: Dashboard → **Import** on `stu-local`, or
`curl -X POST http://127.0.0.1:8000/api/v1/projects/stu-local/imports -H 'Content-Type: application/json' -d '{}'`.

## 5. Stop / reset

```bash
docker compose -f deploy/dev/compose.yml down        # keeps the database volume
docker compose -f deploy/dev/compose.yml down -v     # drops it too
```
