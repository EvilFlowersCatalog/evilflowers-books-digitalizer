# EvilFlowers Books Digitalizer — the portal image (evilflowers-books-digitalizer#6)
#
# One image, two roles (the container command decides):
#   api           uvicorn evilflowers_books_digitalizer.studio.api.app:create_app --factory --host 0.0.0.0 --port 8000
#                 (FastAPI + the SPA bundle + /mcp)
#   orchestrator  celery -A evilflowers_books_digitalizer.studio.workers.app:celery worker -Q digitalizer.portal -B
#                 (the job ledger, beat: pump + reconcile, /metrics on $DIGITALIZER_METRICS_PORT)
#
# No ScanTailor, Tesseract, jbig2enc, recode_pdf, pandoc, DocRes or ONNX models: every pixel
# and model job runs on a service on Valkey db 8 (scan, ocr, analyzer, equation, render;
# knowledge#66). The orchestrator's own pixel work is leaf planning (OpenCV) and the API's
# renditions (Pillow, pypdfium2).
#
# Build:
#   docker build -t evilflowers-digitalizer .
#   docker build --build-arg PROTOCOL_REF=<commit> ...   # another evilflowers-protocol commit
#
# Runtime contract (deploy/k8s relies on it):
#   * `evilflowers-digitalizer` is on PATH for any uid; the default USER is 10001 but any
#     non-root uid:gid works (nothing writes under /app or /opt).
#   * config: $STUDIO_CONFIG (deploy/k8s mounts configs/studio.k8s.toml there);
#     $STUDIO_STORAGE_ROOT = where the shared storage is mounted (the services' STORAGE_PATH).
#   * logs on stderr, EVILFLOWERS_LOG_FORMAT=json for one JSON object per line.
#   * STUDIO_ROLE=api arms the HEALTHCHECK.

# --- SPA (frontend/) ------------------------------------------------------------
# Built separately so node never enters the runtime image; only dist/ is copied.
# Manifests first: the dependency layer is cached until the lockfile changes.
# pnpm-workspace.yaml carries `allowBuilds` (esbuild's install script): without it pnpm
# refuses the install (ERR_PNPM_IGNORED_BUILDS, #10). corepack takes the pnpm version
# pinned in package.json's `packageManager`, not whatever is newest at build time.
FROM node:22-bookworm-slim AS frontend
WORKDIR /src/frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build


# --- portal: api + orchestrator (default target) ----------------------------------
FROM python:3.12-slim-trixie AS portal

# git: evilflowers-protocol is installed from its repository (not on PyPI)
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python3 -m venv /opt/venv

# EvilFlowers Catalog client (not on PyPI; public repo, pinned commit). Installed
# first so `pip install .` finds `evilflowers-protocol` already satisfied.
ARG PROTOCOL_REF=62c05d7650381bfcc3dd8ebeb0d733fc9c83e494
RUN pip install \
      "evilflowers-protocol @ git+https://github.com/EvilFlowersCatalog/evilflowers-protocol-python.git@${PROTOCOL_REF}"

COPY pyproject.toml README.md ./
COPY evilflowers_books_digitalizer ./evilflowers_books_digitalizer
COPY configs ./configs
RUN pip install ".[telemetry]" \
 && evilflowers-digitalizer --help >/dev/null \
 && command -v uvicorn && command -v celery \
 && python -c "import cv2, pypdfium2, lxml, PIL; print('portal ok')" \
 && ! python -c "import onnxruntime" 2>/dev/null \
 && ! command -v tesseract && ! command -v scantailor-deviant-cli

# the SPA bundle (served by the portal; STUDIO_STATIC_DIR below)
COPY --from=frontend /src/frontend/dist /app/frontend/dist

# --- runtime user -------------------------------------------------------------
# The group entry for the default gid keeps Celery's privilege check from assuming root
# (deploy/k8s/base runs 10001:10001); an overlay's other gid is handled in
# studio/workers/app.py (#13).
RUN chmod -R a+rX /opt/venv /app \
 && groupadd --gid 10001 digitalizer \
 && useradd --uid 10001 --gid 0 --groups digitalizer --home-dir /tmp --no-create-home \
      --shell /usr/sbin/nologin digitalizer

# <ref>@<sha> from CI (docker.yml): service.version on every span; the pod's env may override it
ARG IMAGE_TAG=unknown
ENV IMAGE_TAG=${IMAGE_TAG} \
    STUDIO_STATIC_DIR=/app/frontend/dist \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001

# the portal listens here (the orchestrator listens on its metrics port only)
EXPOSE 8000

# Probes the API only when the container runs it (STUDIO_ROLE=api); the orchestrator
# shares the image but serves no API, so it reports healthy rather than flapping. k8s
# ignores HEALTHCHECK and uses its own probes (deploy/k8s). python, not curl: no curl here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python3", "-c", "import os, sys, urllib.request as u; os.environ.get('STUDIO_ROLE') != 'api' and sys.exit(0); u.urlopen('http://127.0.0.1:' + os.environ.get('STUDIO_PORT', '8000') + '/healthz', timeout=4)"]

CMD ["evilflowers-digitalizer", "--help"]
