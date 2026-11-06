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


FROM debian:trixie-slim
ARG SCANTAILOR_DEPS

# jpegoptim: required by recode_pdf for the distribution (JPEG MRC) output.
# qpdf: linearizes the access copy. ghostscript: PDF/A fallback.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev build-essential git ca-certificates \
    tesseract-ocr tesseract-ocr-slk tesseract-ocr-ces tesseract-ocr-eng \
    tesseract-ocr-deu tesseract-ocr-rus \
    $SCANTAILOR_DEPS \
    jpegoptim qpdf ghostscript \
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
 && evilflowers-digitalizer --help >/dev/null

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
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001

CMD ["evilflowers-digitalizer", "--help"]
