# EvilFlowers Books Digitalizer — full tooling image
#
# Contains everything the `scantailor_mrc` pipeline engine shells out to:
#   * scantailor-deviant-cli  (built from source — no distro package)
#   * jbig2enc                (built from source — MRC text-mask compression)
#   * tesseract + slk/ces/eng/deu/rus language packs
#   * archive-pdf-tools (recode_pdf), and the project package itself
#   * DocRes (optional AI enhancement; CPU inference — slow, see pipeline.toml)
#
# Build:
#   docker build -t evilflowers-digitalizer .
#   docker build --build-arg DOCRES_WEIGHTS=0 -t evilflowers-digitalizer .   # skip the ~900 MB weights
#
# Run a batch (mount cache/output/credentials; VPN must reach the NAS):
#   docker run --rm -it \
#     -v $PWD/credentials.toml:/app/credentials.toml:ro \
#     -v $PWD/.cache:/app/.cache \
#     -v $PWD/output:/app/output \
#     evilflowers-digitalizer \
#     python -c "from evilflowers_books_digitalizer.batch import process_book; \
#                print(process_book('fad', '<BOOK_ID>'))"

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

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev build-essential git ca-certificates \
    tesseract-ocr tesseract-ocr-slk tesseract-ocr-ces tesseract-ocr-eng \
    tesseract-ocr-deu tesseract-ocr-rus \
    $SCANTAILOR_DEPS \
    # legacy engine extras (OCRmyPDF)
    ghostscript pngquant unpaper \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/bin/scantailor-deviant-cli /usr/local/bin/
COPY --from=build /usr/local/bin/jbig2 /usr/local/bin/
COPY --from=build /usr/local/lib/ /usr/local/lib/
RUN ldconfig && scantailor-deviant-cli --help >/dev/null && command -v jbig2

# --- project ----------------------------------------------------------------
WORKDIR /app
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN python3 -m venv /opt/venv && pip install --no-cache-dir --upgrade pip

COPY pyproject.toml README.md ./
COPY evilflowers_books_digitalizer ./evilflowers_books_digitalizer
COPY configs ./configs
RUN pip install --no-cache-dir . && recode_pdf --version

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

CMD ["bash"]
