"""PaddleOCR-VL element recognizer — standalone sidecar (no package imports).

Runs under the *tool* venv (torch + transformers>=5), not the pipeline's:

    <venv>/bin/python vlm_worker.py --model <dir> [--device auto|mps|cuda|cpu] jobs.json cache.json

jobs.json: ``[{"id", "image", "bbox": [x0, y0, x1, y1], "task": "ocr"|"formula"}]``.
cache.json is updated after every job (resumable); a job that fails (e.g. GPU
OOM on a pathological crop) is recorded with ``"error"`` and skipped.

Prompt, crop padding and generation live in ``vlm_service/recognition.py`` (shared
with the HTTP service). It is loaded as the top-level package ``vlm_service`` from
the directory above this file, so the tool venv never imports the pipeline package.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# append, not prepend: the package directory must never shadow stdlib / site-packages
sys.path.append(str(Path(__file__).resolve().parents[1]))
from vlm_service import recognition as rec  # noqa: E402


def pending(result: dict | None, max_attempts: int) -> bool:
    """Not run yet, or failed (e.g. OOM) with attempts left."""
    if result is None:
        return True
    return bool(result.get("error")) and result.get("attempts", 1) < max_attempts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--device", default="auto")
    # MPS leaks driver memory across generate() calls ("other allocations" grow until OOM);
    # the caller recycles the process every --max-jobs jobs and retries OOM'd jobs once.
    ap.add_argument("--max-jobs", type=int, default=25)
    ap.add_argument("--attempts", type=int, default=2)
    ap.add_argument("--attn", default="sdpa", help="attention implementation: sdpa | eager")
    ap.add_argument("jobs")
    ap.add_argument("cache")
    a = ap.parse_args()
    jobs = json.loads(Path(a.jobs).read_text())
    cache = Path(a.cache)
    results = json.loads(cache.read_text()) if cache.exists() else {}
    todo = [j for j in jobs if pending(results.get(j["id"]), a.attempts)]
    if a.attn == "sdpa":   # fresh jobs first; failed ones wait for the eager retry round
        todo = [j for j in todo if j["id"] not in results] or todo
    todo = todo[: a.max_jobs]
    if not todo:
        return
    lm = rec.load_model(a.model, device=a.device, attn=a.attn)
    for job in todo:
        t = time.time()
        text, err = "", None
        try:
            text = rec.generate(lm, rec.crop(job["image"], job["bbox"]), job["task"])
        except RuntimeError as exc:
            err = str(exc)[:300]
        finally:
            rec.free_memory(lm.device)
        attempts = (results.get(job["id"]) or {}).get("attempts", 0) + 1
        results[job["id"]] = {"text": text, "task": job["task"], "secs": round(time.time() - t, 2),
                              "attempts": attempts, **({"error": err} if err else {})}
        cache.write_text(json.dumps(results, ensure_ascii=False))
        print(f"{job['id']} {results[job['id']]['secs']}s", flush=True)
        if err and "out of memory" in err:
            # the MPS/CUDA allocator never gives that memory back: every later job in
            # this process would OOM instantly. Exit; the caller starts a fresh worker.
            print("OOM: exiting so the next jobs get a clean process", flush=True)
            break


if __name__ == "__main__":
    main()
