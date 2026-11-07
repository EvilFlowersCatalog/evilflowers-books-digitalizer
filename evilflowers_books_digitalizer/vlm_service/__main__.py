"""``python -m vlm_service`` / ``python -m evilflowers_books_digitalizer.vlm_service``.

Every flag has an environment default (``VLM_*``) so the k8s manifest stays short.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path


def _env_bool(name: str) -> bool | None:
    v = os.environ.get(name)
    return None if v is None or v == "" else v.lower() in {"1", "true", "yes"}


def main() -> None:
    ap = argparse.ArgumentParser(prog="vlm_service", description=__doc__)
    ap.add_argument("--host", default=os.environ.get("VLM_HOST", "0.0.0.0"))  # noqa: S104
    ap.add_argument("--port", type=int, default=int(os.environ.get("VLM_PORT", "8000")))
    ap.add_argument("--model-dir", type=Path, default=Path(os.environ.get("VLM_MODEL_DIR", "/models")))
    ap.add_argument("--device", default=os.environ.get("VLM_DEVICE", "cpu"))
    ap.add_argument("--dtype", default=os.environ.get("VLM_DTYPE", "auto"),
                    help="auto (float32 on CPU) | float32 | bfloat16")
    ap.add_argument("--threads", type=int,
                    default=int(os.environ.get("VLM_THREADS", os.environ.get("OMP_NUM_THREADS", "0"))),
                    help="torch intra-op threads; 0 = torch default (HOST cores, not the cgroup limit)")
    ap.add_argument("--max-queue", type=int, default=int(os.environ.get("VLM_MAX_QUEUE", "2")),
                    help="requests allowed to wait behind the running one before 503")
    # The checkpoint's generation_config says use_cache=false, which re-runs the whole
    # sequence per token. With the KV cache: identical text, ~3.6x faster on CPU.
    cache = ap.add_mutually_exclusive_group()
    cache.add_argument("--kv-cache", dest="use_cache", action="store_true")
    cache.add_argument("--no-kv-cache", dest="use_cache", action="store_false")
    env_cache = _env_bool("VLM_KV_CACHE")
    ap.set_defaults(use_cache=True if env_cache is None else env_cache)
    a = ap.parse_args()

    logging.basicConfig(level=os.environ.get("VLM_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn

    from .app import create_app
    from .engine import PaddleEngine

    engine = PaddleEngine(a.model_dir, device=a.device, dtype=a.dtype, threads=a.threads,
                          use_cache=a.use_cache)
    uvicorn.run(create_app(engine, max_queue=a.max_queue), host=a.host, port=a.port,
                log_level=os.environ.get("VLM_LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
