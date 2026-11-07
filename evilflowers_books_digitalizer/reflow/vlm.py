"""Vision-model sidecar for math, plus everything that keeps its output honest.

The model (PaddleOCR-VL, Apache-2.0) runs out of process in its own venv —
same pattern as the DocRes step — via :mod:`vlm_worker` (a standalone script
with no package imports). Results are cached per book in a JSON file keyed by
``page + bbox + task``, so re-analysis never attaches a stale answer to a
different region.

Output guards:

* :func:`looks_hallucinated` — a drawing read as "math" (stacked arrays of
  lone labels, caption text inside math);
* :func:`fix_latex` — normalizes environments texmath/LaTeX accept;
* :func:`validate_math` — one batched pandoc run proves which snippets become
  MathML; the rest fall back (image crop or Tesseract text).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from evilflowers_books_digitalizer.reflow.model import Box

logger = logging.getLogger(__name__)

WORKER = Path(__file__).with_name("vlm_worker.py")
JOBS_PER_PROCESS = 25   # recycle the worker process (MPS memory leak)
MAX_ATTEMPTS = 2        # an OOM'd job gets one more try in a fresh process

type Task = Literal["ocr", "formula"]


@dataclass(frozen=True, slots=True)
class Job:
    page: int
    image: Path
    bbox: Box
    task: Task

    @property
    def key(self) -> str:
        return f"p{self.page:04d}_{self.task}_" + "_".join(str(v) for v in self.bbox)


@dataclass(slots=True)
class VlmSettings:
    python: Path
    model: Path
    device: str = "auto"
    timeout_s: int = 6 * 3600


def run_jobs(jobs: list[Job], cache: Path, settings: VlmSettings) -> dict[str, str]:
    """Run uncached jobs through the worker; return ``{job.key: text}`` for all jobs.

    Failed jobs (OOM, timeouts) are absent from the result — callers fall back.
    """
    def load() -> dict[str, dict[str, Any]]:
        return json.loads(cache.read_text()) if cache.exists() else {}

    def pending(results: dict[str, dict[str, Any]]) -> list[Job]:
        return [j for j in jobs if j.key not in results or (
            results[j.key].get("error") and results[j.key].get("attempts", 1) < MAX_ATTEMPTS)]

    results = load()
    todo = pending(results)
    if todo and (not settings.python.exists() or not settings.model.exists()):
        raise RuntimeError(f"VLM not installed (python={settings.python}, model={settings.model})")
    spec = cache.with_suffix(".jobs.json")
    spec.write_text(json.dumps([
        {"id": j.key, "image": str(j.image), "bbox": list(j.bbox), "task": j.task} for j in jobs
    ]))
    # one fresh worker per round of JOBS_PER_PROCESS jobs: MPS leaks driver memory
    # across generate() calls, and a new process is the only reliable way to free it
    while todo:
        # first attempts use fast SDPA attention; a job that OOM'd (MPS caches a graph
        # per sequence length and runs out on long generations) is retried with eager
        # attention — ~10x slower but bounded memory
        retry = all(j.key in results for j in todo)
        proc = subprocess.run(
            [str(settings.python), str(WORKER), "--model", str(settings.model),
             "--device", settings.device, "--max-jobs", str(JOBS_PER_PROCESS),
             "--attempts", str(MAX_ATTEMPTS), "--attn", "eager" if retry else "sdpa",
             str(spec), str(cache)],
            capture_output=True, text=True, timeout=settings.timeout_s,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"VLM worker failed (exit {proc.returncode}): {proc.stderr[-1500:]}")
        before = sum(int(r.get("attempts", 1)) for r in results.values())
        results = load()
        # progress = attempts consumed (a failed first try still moves toward the retry)
        if sum(int(r.get("attempts", 1)) for r in results.values()) <= before:
            break                         # the worker did nothing: give up, callers fall back
        todo = pending(results)
    failed = sum(1 for j in jobs if results.get(j.key, {}).get("error"))
    if failed:
        logger.warning("VLM: %d of %d jobs failed after retries (falling back to images)", failed, len(jobs))
    return {j.key: results[j.key]["text"] for j in jobs
            if j.key in results and not results[j.key].get("error") and results[j.key]["text"].strip()}


# ---------------------------------------------------------------- LaTeX hygiene
def strip_delims(tex: str) -> str:
    tex = tex.strip()
    for open_, close in (("\\[", "\\]"), ("$$", "$$"), ("\\(", "\\)"), ("$", "$")):
        if tex.startswith(open_) and tex.endswith(close) and len(tex) > len(open_) + len(close):
            return tex[len(open_):-len(close)].strip()
    return tex


def fix_latex(tex: str) -> str:
    tex = strip_delims(tex)
    tex = re.sub(r"\\eqno\s*\{?\(?([\w.]+)\)?\}?", r"\\tag{\1}", tex)   # texmath knows \tag
    for env in ("align*", "align"):
        tex = tex.replace(f"\\begin{{{env}}}", "\\begin{aligned}").replace(f"\\end{{{env}}}", "\\end{aligned}")
    return tex


def split_tag(tex: str) -> tuple[str, str]:
    """``\\tag{X}`` is display-only: lift it out (returns tex-without-tag, X)."""
    m = re.search(r"\\tag\{([^}]*)\}", tex)
    return re.sub(r"\s*\\tag\{[^}]*\}", "", tex).strip(), (m[1] if m else "")


def looks_hallucinated(tex: str) -> bool:
    """Formula-model output that describes a drawing rather than math."""
    if re.search(r"Obr\.|Obrázok|Tab\.", tex):
        return True
    arrays = re.findall(r"\\begin\{array\}\{[lcr]+\}(.*?)\\end\{array\}", tex, re.S)
    if len(arrays) >= 2:
        cells = [c.strip() for a in arrays for c in re.split(r"\\\\|&", a) if c.strip()]
        # a LaTeX command (\beta) is one symbol; lone labels are 1-2 symbols
        size = [len(re.sub(r"[\\{}_^ ]", "", re.sub(r"\\[a-zA-Z]+", "x", c))) for c in cells]
        if cells and sum(n <= 2 for n in size) / len(cells) > 0.7:
            return True
    return bool(re.search(r"\\xrightarrow|\\longrightarrow", tex)) and len(tex) < 40


def validate_math(snippets: set[str], pandoc: str = "pandoc") -> set[str]:
    """Return the subset of display-math snippets pandoc/texmath converts to MathML.

    One pandoc call for the whole book: each snippet is a numbered paragraph; a
    snippet whose paragraph holds ``<math`` converted, otherwise texmath gave up.
    Inline snippets are prefixed with ``"$"`` by the caller and checked inline.
    """
    items = sorted(snippets)
    if not items:
        return set()
    md = "\n\n".join(
        (f"S{i}: ${s[1:]}$" if s.startswith("$") else f"S{i}:\n\n$${s}$$")
        for i, s in enumerate(items)
    )
    proc = subprocess.run([pandoc, "-f", "markdown", "-t", "html", "--mathml"],
                          input=md, capture_output=True, text=True)
    if proc.returncode != 0:
        logger.warning("pandoc math validation failed: %s", proc.stderr[-500:])
        return set()
    html = proc.stdout
    ok = set()
    marks = [m.start() for m in re.finditer(r"<p>S\d+:", html)] + [len(html)]
    for start, end in zip(marks, marks[1:], strict=False):
        idx = int(re.match(r"<p>S(\d+):", html[start:])[1])  # type: ignore[index]
        if "<math" in html[start:end]:
            ok.add(items[idx])
    return ok
