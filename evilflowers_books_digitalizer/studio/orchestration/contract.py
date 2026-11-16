"""The contract with standalone (catalog-free) service tasks.

What a service task receives (keyword arguments, JSON) and what it leaves behind (an
outcome file, written last and atomically by ``evilflowers_worker.outcome``). The
generic half is evilflowers-worker#5 (``StandaloneJob``); the per-stage halves are
defined here, because the orchestrator owns the rows the results land in.

Every path in a message or an outcome is **relative to the services' ``STORAGE_PATH``**
(``StudioConfig.storage_root``). A worker writes only under ``output_dir``, a directory
that belongs to one attempt of one job (``<item>/<item>_studio/jobs/<job id>-a<attempt>/``), so a
superseded or duplicate attempt can never overwrite what another attempt produced.
Whether an attempt's output becomes the page is decided by the orchestrator, when it
applies the outcome (token and task id must match the current attempt).

Message (``StandaloneJob`` + the stage's fields)::

    {"run_id": "42", "job": "clean-0003-a1", "token": "…", "output_dir": "…/jobs/17-a1",
     "outcome": "…/jobs/17-a1/outcome.json", "inputs": […], …}

Outcome (``status`` = done | skipped | failed; ``error`` set when failed)::

    {"run_id": "42", "token": "…", "status": "done", "error": null, …}
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evilflowers_books_digitalizer.db.models import Job, Page, Region, Run
from evilflowers_books_digitalizer.studio.domain import BBox
from evilflowers_books_digitalizer.studio.ports import PortError

OUTCOME_FILE = "outcome.json"
#: the directory, inside ``<item>/<item>_studio/``, that holds every attempt's output dir
JOBS_DIR = "jobs"


class OutcomeError(PortError):
    """An outcome file that is missing, unreadable, or not for this attempt."""


class Outcome(BaseModel):
    """The fields every outcome carries (``evilflowers_worker.outcome.OutcomeFile``)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    run_id: str
    token: str
    status: Literal["done", "skipped", "failed"]
    error: str | None = None


# ---------------------------------------------------------------- stage: clean
class Mark(BaseModel):
    """A scan artefact candidate from scan-service's check (``leaves[].marks``,
    evilflowers-scan-service#2). Kept on the page until analyze fuses it as a proposal."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    bbox: tuple[int, int, int, int]   # cleaned-page pixels, x1/y1 exclusive
    category: str                     # stamp | sticker | bleed_through | edge | content
    p: float = Field(ge=0.0, le=1.0)
    band: Literal["keep", "review", "hide"] = "review"
    model: str | None = None          # "signals", or the decider's model@revision
    reason: str | None = None


class CleanedLeaf(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    leaf: int
    image: str                  # cleaned page (lossless), relative to STORAGE_PATH
    width: int
    height: int
    dpi: int
    skew_deg: float = 0.0
    preview: str | None = None  # WebP for the SPA
    dzi: str                    # Deep Zoom descriptor (its _files/ next to it)
    #: the crop actually cut (IA x,y,w,h, rotated-frame px): clamped, split_x or override
    crop_box: tuple[int, int, int, int] | None = None
    warnings: list[str] = []
    marks: list[Mark] = []


class CleanOutcome(Outcome):
    leaves: list[CleanedLeaf] = []


# ---------------------------------------------------------------- stage: analyze (split)
# Masks: page-pixel boxes [x0, y0, x1, y1] in the cleaned page's space. A service paints
# them white (as ``ImageOps.erase``) on its PRIVATE copy before reading; the page itself
# is never changed by hocr / layout.
class HocrLeaf(BaseModel):
    """``evilflowers_ocr_worker.standalone.hocr``: one leaf's OCR (evilflowers-ocr-service#7)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    leaf: int
    hocr: str                   # one ocr_page (IA id page_%06d), relative to STORAGE_PATH
    text: str
    mean_conf: float            # 0..100 over words


class HocrOutcome(Outcome):
    leaves: list[HocrLeaf] = []


class LayoutRegion(BaseModel):
    """One row of ``regions.json`` (evilflowers-analyzer-service#8)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    leaf: int
    kind: str                   # Docling label (picture, table, formula, caption, text, …)
    bbox: tuple[float, float, float, float]   # x0, y0, x1, y1: fractions, origin top-left
    seq: int = 0
    confidence: float = 1.0
    caption: str | None = None


class LayoutOutcome(Outcome):
    regions: str | None = None  # regions.json, relative to STORAGE_PATH (done: required)
    leaves: list[int] = []      # every leaf the task looked at (a leaf may have no regions)


# ---------------------------------------------------------------- stage: formulas
class FormulaAnswer(BaseModel):
    """One row of ``formulas.json`` (evilflowers-equation-service#8)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    key: str                    # the region key the message gave (``region-<id>``)
    text: str | None = None     # LaTeX (task formula) or text with inline math (task ocr)
    model: str | None = None    # provenance, e.g. "paddleocr-vl:<revision>"


class FormulasOutcome(Outcome):
    formulas: str | None = None  # formulas.json, relative to STORAGE_PATH (done: required)


# ---------------------------------------------------------------- stage: render (chained)
class RenderedFile(BaseModel):
    """One medium ``evilflowers_render.book`` produced (evilflowers-render-service#1)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    path: str                   # in output_dir, relative to STORAGE_PATH
    size: int
    md5: str | None = None


class RenderOutcome(Outcome):
    #: artifact kind (pdf | pdfa | epub | images) -> file; every requested format is required
    files: dict[str, RenderedFile] = {}
    pdfa_valid: bool | None = None   # the PDF/A check (veraPDF), when it ran


#: outcome model per job kind (``stage`` or ``stage.part``) a service may run
OUTCOMES: dict[str, type[Outcome]] = {
    "clean": CleanOutcome,
    "analyze.hocr": HocrOutcome,
    "analyze.layout": LayoutOutcome,
    "analyze.erase": CleanOutcome,       # an erased page is a new cleaned page
    "formulas": FormulasOutcome,
    "render.book": RenderOutcome,
}


def part(job: Job) -> str | None:
    """The part of a split stage this job runs (``hocr`` / ``layout`` / ``erase``), if any."""
    value = (job.params or {}).get("part")
    return str(value) if value else None


def kind(job: Job) -> str:
    """The key of a job's contract: ``clean``, ``analyze.hocr``, …"""
    return f"{job.stage}.{p}" if (p := part(job)) else job.stage


def job_key(job: Job) -> str:
    """Stable, short, human-readable: ``clean-0003-a1``, ``analyze-hocr-0003-a1``
    (≤ 30 chars, the kit's job-name limit; the part keeps a batch's jobs apart)."""
    middle = f"{job.stage}-{p}" if (p := part(job)) else job.stage
    return f"{middle}-{job.seq:04d}-a{job.attempt}"


class Storage:
    """Paths between the orchestrator's view (absolute) and the services' (relative)."""

    def __init__(self, root: Path | None) -> None:
        self.root = root

    def _root(self) -> Path:
        if self.root is None:
            raise PortError("a service-backed stage needs [studio] storage_root "
                            "(the services' STORAGE_PATH)")
        return self.root

    def rel(self, path: Path | str) -> str:
        p = Path(path)
        try:
            return p.resolve().relative_to(self._root().resolve()).as_posix()
        except ValueError:
            raise PortError(f"{p} is outside the shared storage {self._root()}") from None

    def abs(self, rel: str) -> Path:
        root = self._root().resolve()
        p = (root / rel).resolve()
        if not p.is_relative_to(root) or p == root:
            raise OutcomeError(f"path escapes the shared storage: {rel!r}")
        return p

    def job_dir(self, item_dir: Path, job: Job) -> str:
        return self.rel(item_dir / f"{item_dir.name}_studio" / JOBS_DIR / f"{job.id}-a{job.attempt}")


def attempt_dir(path: str | Path | None) -> Path | None:
    """The attempt dir a page file lives in (``…/jobs/<job>-a<n>/[sub/…]<file>``), else None."""
    if not path:
        return None
    return next((d for d in Path(path).parents if d.parent.name == JOBS_DIR), None)


def input_paths(fields: dict[str, Any]) -> list[str]:
    """The kit's ``StandaloneJob.inputs`` from a stage's structured fields: every file the job
    reads, relative to ``STORAGE_PATH``, in order, each once (#9).

    clean: each leaf's ``frame``; hocr, layout, erase: each leaf's cleaned ``image``; formulas:
    the distinct ``image`` of its regions; render: the ``request`` (it lists the pages)."""
    paths: list[str] = []
    for row in [*fields.get("leaves", ()), *fields.get("regions", ())]:
        paths.append(row.get("frame") or row["image"])
    if fields.get("request"):
        paths.append(fields["request"])
    return list(dict.fromkeys(paths))


def message(job: Job, run: Run, token: str, storage: Storage, item_dir: Path,
            inputs: dict[str, Any]) -> dict[str, Any]:
    """The keyword arguments of a service task: the kit's ``StandaloneJob`` (with its required
    ``inputs`` list) + the stage's structured fields, which the services read per leaf."""
    paths = input_paths(inputs)
    if not paths:
        raise PortError(f"job {job.id} ({kind(job)}) has no input file")
    output_dir = storage.job_dir(item_dir, job)
    return {"run_id": str(run.id), "job": job_key(job), "token": token, "inputs": paths,
            "output_dir": output_dir, "outcome": f"{output_dir}/{OUTCOME_FILE}", **inputs}


def clean_inputs(pages: list[Page], storage: Storage,
                 settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """What ``evilflowers_scan.clean`` needs: the persisted leaf plan of each page + overrides,
    and the project's ScanTailor settings (``[projects.X.pipeline.scantailor]``: color_mode,
    margins_mm, dpi, output_dpi, …), which the in-process cleaner reads from the project."""
    leaves = []
    for p in pages:
        if not p.source_path or not p.crop_box:
            raise PortError(f"leaf {p.index} of book {p.book_pk} was never planned")
        leaves.append({"leaf": p.index, "frame": storage.rel(p.source_path), "side": p.side,
                       "crop_box": list(p.crop_box), "rotate_degree": p.rotate_degree or 0,
                       "hand_side": p.hand_side, "override": dict(p.override or {})})
    return {"leaves": leaves, **({"scantailor": dict(settings)} if settings else {})}


def _cleaned(page: Page, storage: Storage) -> str:
    if not page.clean_path:
        raise PortError(f"leaf {page.index} of book {page.book_pk} has no cleaned page")
    return storage.rel(page.clean_path)


def _boxes(boxes: Iterable[BBox]) -> list[list[int]]:
    return [[b.x0, b.y0, b.x1, b.y1] for b in boxes]


def hocr_inputs(pages: list[Page], storage: Storage, *, language: Callable[[Page], str],
                mask: Callable[[Page], list[BBox]]) -> dict[str, Any]:
    """``evilflowers_ocr_worker.standalone.hocr``: cleaned leaves, masked, with OCR settings."""
    return {"leaves": [{"leaf": p.index, "image": _cleaned(p, storage), "dpi": p.dpi or 300,
                        "language": language(p), "mask": _boxes(mask(p))} for p in pages]}


def layout_inputs(pages: list[Page], storage: Storage, *,
                  mask: Callable[[Page], list[BBox]]) -> dict[str, Any]:
    """``evilflowers_analyzer.standalone.layout``: cleaned leaves, masked."""
    return {"leaves": [{"leaf": p.index, "image": _cleaned(p, storage), "mask": _boxes(mask(p))}
                       for p in pages]}


def erase_inputs(pages: list[Page], storage: Storage, *,
                 boxes: Callable[[Page], list[BBox]]) -> dict[str, Any]:
    """``evilflowers_scan.erase``: paint ``boxes`` white on the page itself (a new image)."""
    return {"leaves": [{"leaf": p.index, "image": _cleaned(p, storage), "dpi": p.dpi or 300,
                        "boxes": _boxes(boxes(p))} for p in pages]}


def formulas_inputs(pages: list[Page], storage: Storage, *,
                    regions: Callable[[Page], list[Region]],
                    task: Callable[[Region], str]) -> dict[str, Any]:
    """``evilflowers_equation.standalone.formulas``: the regions to (re-)read, as crops of the
    cleaned page. ``bbox`` in fractions, origin top-left (as the analyzer reports them)."""
    rows = []
    for p in pages:
        w, h = p.width or 0, p.height or 0
        if not w or not h:
            raise PortError(f"leaf {p.index} of book {p.book_pk} has no size")
        rows += [{"key": f"region-{r.id}", "leaf": p.index, "image": _cleaned(p, storage),
                  "bbox": [r.x0 / w, r.y0 / h, r.x1 / w, r.y1 / h], "task": task(r)}
                 for r in regions(p)]
    return {"regions": rows}


def read_outcome(storage: Storage, job: Job, stage_model: type[Outcome]) -> Outcome:
    """The attempt's outcome, checked against the job (run, token); raises :class:`OutcomeError`."""
    rel = (job.params or {}).get("outcome")
    if not rel:
        raise OutcomeError(f"job {job.id} has no outcome path")
    path = storage.abs(rel)
    try:
        outcome = stage_model.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise OutcomeError(f"job {job.id}: no outcome file at {rel}") from None
    except (OSError, ValueError, ValidationError) as exc:
        raise OutcomeError(f"job {job.id}: unreadable outcome {rel}: {exc}") from exc
    if outcome.run_id != str(job.run_pk) or outcome.token != job.token:
        raise OutcomeError(f"job {job.id}: outcome {rel} belongs to another attempt")
    return outcome

