# EvilFlowers Books Digitalizer — full tooling image
#
# Contains everything the `scantailor_mrc` pipeline engine shells out to:
#   * scantailor-deviant-cli  (built from source — no distro package)
#   * jbig2enc                (built from source — MRC text-mask compression)
#   * tesseract + slk/ces/eng/deu/rus language packs
#   * archive-pdf-tools (recode_pdf), and the project package itself
#   * DocRes (optional AI enhancement; CPU inference — slow, see pipeline.toml)
#
# This image also runs the batch (see deploy/docker-compose.yml): `pip install .`
# pulls in pandas/openpyxl/rich and bundles the cover fonts, so the container can
# run `python -m evilflowers_books_digitalizer run-corpus` directly.
#
# Build:
#   docker build -t evilflowers-digitalizer .
#   docker build --build-arg DOCRES_WEIGHTS=0 -t evilflowers-digitalizer .   # skip the ~900 MB weights
#   docker build --build-arg PROTOCOL_REF=<commit> ...   # another evilflowers-protocol commit
#
# Runtime contract (the k8s Job relies on it):
#   * `evilflowers-digitalizer` is on PATH for any uid; the default USER is 10001
#     but any non-root uid:gid works (nothing writes under /app or /opt).
#   * config: --config > $EVILFLOWERS_DIGITALIZER_CONFIG (default below) ;
#     relative paths in it resolve against the config file's directory.
#   * logs on stderr, EVILFLOWERS_LOG_FORMAT=json for one JSON object per line.
#
# Run one book (mount scans read-only, output/cache writable, config read-only):
#   docker run --rm --user "$(id -u):$(id -g)" \
#     -v /mnt/digital-library/raw-scans:/data/raw-scans:ro \
#     -v $PWD/output:/data/output -v $PWD/.cache:/data/cache \
#     -v $PWD/my-pipeline.toml:/etc/digitalizer/pipeline.toml:ro \
#     -e EVILFLOWERS_DIGITALIZER_CONFIG=/etc/digitalizer/pipeline.toml \
#     evilflowers-digitalizer run-book fad '<BOOK_ID>'
#
# Restoration studio (deploy/k8s): the SAME image runs the API and every Celery
# worker; the container command decides the role (one build, one toolchain, so a
# page rendered by a worker and re-rendered interactively never diverge):
#   api:     uvicorn evilflowers_books_digitalizer.studio.api.app:create_app --factory --host 0.0.0.0 --port 8000
#   workers: celery -A evilflowers_books_digitalizer.studio.workers.app:celery worker -Q <queues>
# The SPA (frontend/, React + Vite) is built in its own stage and served by the
# API from $STUDIO_STATIC_DIR. Set STUDIO_ROLE=api to arm the HEALTHCHECK.

# dev packages double as runtime deps below — exact runtime soname package
# names churn across Debian releases (t64 transition); -dev names are stable.
ARG SCANTAILOR_DEPS="qtbase5-dev libqt5svg5-dev libtiff-dev libjpeg62-turbo-dev \
    libpng-dev zlib1g-dev libopenjp2-7-dev libexiv2-dev libcanberra-dev \
    libmupdf-dev libharfbuzz-dev libjbig2dec0-dev libmujs-dev libgumbo-dev \
    libfreetype-dev libleptonica-dev"

FROM debian:trixie-slim AS build
ARG SCANTAILOR_DEPS

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates build-essential cmake pkg-config \
    qttools5-dev libboost-dev automake libtool \
    $SCANTAILOR_DEPS \
    && rm -rf /var/lib/apt/lists/*

# scantailor-deviant-cli (CXX 17: distro Eigen needs >= C++14)
RUN apt-get update && apt-get install -y --no-install-recommends libeigen3-dev \
 && git clone --depth 1 \
      https://github.com/ImageProcessing-ElectronicPublications/scantailor-deviant.git /src/st \
 && sed -i 's/SET(CMAKE_CXX_STANDARD 11)/SET(CMAKE_CXX_STANDARD 17)/' /src/st/CMakeLists.txt \
 && cmake -S /src/st -B /build/st -DCMAKE_BUILD_TYPE=Release \
 && make -C /build/st -j"$(nproc)" scantailor-deviant-cli \
 && install -m 755 /build/st/src/app_cli/scantailor-deviant-cli /usr/local/bin/

# jbig2enc (no Debian package)
RUN git clone --depth 1 https://github.com/agl/jbig2enc.git /src/jbig2enc \
 && cd /src/jbig2enc && ./autogen.sh && ./configure && make -j"$(nproc)" && make install


# --- SPA (frontend/) ------------------------------------------------------------
# Built separately so node never enters the runtime image; only dist/ is copied.
# Manifests first: the dependency layer is cached until the lockfile changes.
FROM node:22-bookworm-slim AS frontend
WORKDIR /src/frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build


# --- vlm: PaddleOCR-VL formula service (separate image) ---------------------------
# docker build --target vlm -t ghcr.io/evilflowerscatalog/evilflowers-books-digitalizer-vlm .
#
# CPU-only torch + transformers, pinned to the tool venv the model was validated in
# (venv-docres: torch 2.12.0, transformers 5.22.2). Serves POST /v1/formulas on :8000
# (evilflowers_books_digitalizer/vlm_service, copied as the top-level package
# `vlm_service`: it imports nothing else from the project, so the image carries none
# of the studio's dependencies).
#
# Weights are BAKED IN at a pinned Hugging Face revision rather than fetched into a PVC
# by an init container: the image is then immutable and self-describing (the tag pins
# code + weights together), the pod needs no network, no PVC coordination and works
# with readOnlyRootFilesystem; the cost is ~1.9 GB more per pull, once per node.
# Only the safetensors weights, JSON configs, tokenizer and chat template are fetched:
# no pickled checkpoints and no remote code (*.py) — the model class is native in
# transformers >= 5, and nothing passes trust_remote_code.
#
# Stage order matters: this stage sits BEFORE the studio image so a plain
# `docker build .` (CI) still builds the studio image (the last stage).
FROM python:3.12-slim-trixie AS vlm
ARG VLM_REPO=PaddlePaddle/PaddleOCR-VL-1.6
ARG VLM_REVISION=132c45f52c0a26ed5a6b2f6bd8e3985794cc27dd
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
      torch==2.12.0 torchvision==0.27.0 \
 && pip install transformers==5.22.2 tokenizers==0.23.3 safetensors==0.8.0 \
      huggingface_hub==2.4.0 pillow==12.2.0 "fastapi==0.162.0" "uvicorn==0.54.0" \
 && python -c "import torch; assert not torch.cuda.is_available(); print(torch.__version__)"
RUN python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download('${VLM_REPO}', revision='${VLM_REVISION}', local_dir='/models/PaddleOCR-VL-1.6', \
    allow_patterns=['*.safetensors', '*.json', 'tokenizer.model', 'chat_template.jinja', 'LICENSE'])" \
 && rm -rf /models/PaddleOCR-VL-1.6/.cache \
 && test -f /models/PaddleOCR-VL-1.6/model.safetensors \
 && ! find /models -name '*.bin' -o -name '*.pt' -o -name '*.pth' -o -name '*.pkl' -o -name '*.py' | grep . \
 && chmod -R a+rX /models
COPY evilflowers_books_digitalizer/vlm_service /app/vlm_service
RUN useradd --uid 10001 --gid 0 --home-dir /tmp --no-create-home --shell /usr/sbin/nologin vlm
WORKDIR /app
ENV VLM_MODEL_DIR=/models/PaddleOCR-VL-1.6 \
    VLM_DEVICE=cpu \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/readyz', timeout=4)"]
CMD ["python", "-m", "vlm_service"]


# --- studio: API + every Celery worker (default target) ---------------------------
FROM debian:trixie-slim AS studio
ARG SCANTAILOR_DEPS

# jpegoptim: required by recode_pdf for the distribution (JPEG MRC) output.
# qpdf: linearizes the access copy. ghostscript: PDF/A fallback.
# pandoc (+ potrace, hunspell-sk): the [reflow] EPUB edition.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev build-essential git ca-certificates \
    tesseract-ocr tesseract-ocr-slk tesseract-ocr-ces tesseract-ocr-eng \
    tesseract-ocr-deu tesseract-ocr-rus \
    $SCANTAILOR_DEPS \
    jpegoptim qpdf ghostscript \
    pandoc potrace hunspell hunspell-sk \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/bin/scantailor-deviant-cli /usr/local/bin/
COPY --from=build /usr/local/bin/jbig2 /usr/local/bin/
COPY --from=build /usr/local/lib/ /usr/local/lib/
RUN ldconfig && scantailor-deviant-cli --help >/dev/null && command -v jbig2 && command -v jpegoptim

# --- project ----------------------------------------------------------------
WORKDIR /app
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN python3 -m venv /opt/venv && pip install --no-cache-dir --upgrade pip

# EvilFlowers Catalog client (not on PyPI; public repo, pinned commit). Installed
# first so `pip install .` finds `evilflowers-protocol` already satisfied.
ARG PROTOCOL_REF=62c05d7650381bfcc3dd8ebeb0d733fc9c83e494
RUN pip install --no-cache-dir \
      "evilflowers-protocol @ git+https://github.com/EvilFlowersCatalog/evilflowers-protocol-python.git@${PROTOCOL_REF}"

COPY pyproject.toml README.md ./
COPY evilflowers_books_digitalizer ./evilflowers_books_digitalizer
COPY configs ./configs
RUN pip install --no-cache-dir . && recode_pdf --version \
 && evilflowers-digitalizer --help >/dev/null \
 && command -v pandoc && command -v potrace && command -v hunspell \
 && test -f /usr/share/hunspell/sk_SK.dic \
 && command -v uvicorn && command -v celery

# the SPA bundle (served by studio-api; STUDIO_STATIC_DIR below)
COPY --from=frontend /src/frontend/dist /app/frontend/dist

# --- layout detection (worker-ai): DocLayout-YOLO as ONNX ---------------------
# onnxruntime is not in pyproject yet (see the vlm/layout handoff); the model path is
# what configs/studio.k8s.toml [pipeline.layout] model points at. sha256-checked.
ARG DOCLAYOUT_URL=https://huggingface.co/wybxc/DocLayout-YOLO-DocStructBench-onnx/resolve/main/doclayout_yolo_docstructbench_imgsz1024.onnx
ARG DOCLAYOUT_SHA256=fece9af02f618b603ff7921ccec6861d13e7e1f9830e091dfb7e8ad9311e5b21
RUN pip install --no-cache-dir "onnxruntime>=1.31,<2.0" \
 && mkdir -p /opt/evilflowers-tools/models/doclayout-yolo \
 && python3 -c "\
import hashlib, sys, urllib.request; \
dst = '/opt/evilflowers-tools/models/doclayout-yolo/doclayout_yolo_docstructbench_imgsz1024.onnx'; \
urllib.request.urlretrieve('${DOCLAYOUT_URL}', dst); \
got = hashlib.sha256(open(dst, 'rb').read()).hexdigest(); \
sys.exit(0 if got == '${DOCLAYOUT_SHA256}' else f'sha256 mismatch: {got}')" \
 && python3 -c "import onnxruntime as ort; ort.InferenceSession('/opt/evilflowers-tools/models/doclayout-yolo/doclayout_yolo_docstructbench_imgsz1024.onnx', providers=['CPUExecutionProvider'])"

# --- DocRes (optional, CPU) -------------------------------------------------
ARG DOCRES_WEIGHTS=1
RUN git clone --depth 1 https://github.com/ZZZHANG-jx/DocRes.git /opt/evilflowers-tools/DocRes \
 && sed -i "s/if DEVICE.type == 'cpu':/if DEVICE.type != 'cuda':/" \
      /opt/evilflowers-tools/DocRes/inference.py \
 && python3 -m venv /opt/evilflowers-tools/venv-docres \
 && /opt/evilflowers-tools/venv-docres/bin/pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      torch torchvision numpy opencv-python-headless scikit-image einops tqdm huggingface_hub
RUN if [ "$DOCRES_WEIGHTS" = "1" ]; then \
      /opt/evilflowers-tools/venv-docres/bin/python -c "\
from pathlib import Path; from huggingface_hub import hf_hub_download; import shutil; \
root = Path('/opt/evilflowers-tools/DocRes'); \
(root / 'checkpoints').mkdir(exist_ok=True); \
(root / 'data/MBD/checkpoint').mkdir(parents=True, exist_ok=True); \
shutil.copy(hf_hub_download('presencesw/DocRes', 'docres.pkl'), root / 'checkpoints/docres.pkl'); \
shutil.copy(hf_hub_download('presencesw/DocRes', 'mbd.pkl'), root / 'data/MBD/checkpoint/mbd.pkl')"; \
    fi

# picked up by the pipeline factory as [docres] repo/python defaults
ENV EVILFLOWERS_DOCRES_REPO=/opt/evilflowers-tools/DocRes \
    EVILFLOWERS_DOCRES_PYTHON=/opt/evilflowers-tools/venv-docres/bin/python

# --- runtime user -------------------------------------------------------------
# Everything the pipeline reads at runtime (venvs, DocRes, tessdata, bundled
# fonts/logos, the default config) must be readable — never writable — by an
# arbitrary uid: the k8s Job runs as the owner of the scans, unknown here.
RUN chmod -R a+rX /opt/venv /opt/evilflowers-tools /app /usr/share/tesseract-ocr \
 && useradd --uid 10001 --gid 0 --home-dir /tmp --no-create-home --shell /usr/sbin/nologin digitalizer

ENV EVILFLOWERS_DIGITALIZER_CONFIG=/app/configs/pipeline.toml \
    STUDIO_STATIC_DIR=/app/frontend/dist \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001

# studio-api listens here (workers listen on nothing)
EXPOSE 8000

# Probes the API only when the container runs it (STUDIO_ROLE=api); a worker
# container shares the image but has no HTTP port, so it reports healthy rather
# than flapping. k8s ignores HEALTHCHECK and uses its own probes (deploy/k8s).
# python, not curl: the image has no curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python3", "-c", "import os, sys, urllib.request as u; os.environ.get('STUDIO_ROLE') != 'api' and sys.exit(0); u.urlopen('http://127.0.0.1:' + os.environ.get('STUDIO_PORT', '8000') + '/healthz', timeout=4)"]

CMD ["evilflowers-digitalizer", "--help"]
