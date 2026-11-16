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
import shutil
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from evilflowers_books_digitalizer.db.models import Job, Page, Run
from evilflowers_books_digitalizer.studio.ports import PortError

OUTCOME_FILE = "outcome.json"


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


class CleanOutcome(Outcome):
    leaves: list[CleanedLeaf] = []


#: outcome model per stage that a service may run (a stage without one stays local)
OUTCOMES: dict[str, type[Outcome]] = {"clean": CleanOutcome}


def job_key(job: Job) -> str:
    """Stable, short, human-readable: ``clean-0003-a1`` (≤ 30 chars, the kit's job-name limit)."""
    return f"{job.stage}-{job.seq:04d}-a{job.attempt}"


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
        return self.rel(item_dir / f"{item_dir.name}_studio" / "jobs" / f"{job.id}-a{job.attempt}")


def message(job: Job, run: Run, token: str, storage: Storage, item_dir: Path,
            inputs: dict[str, Any]) -> dict[str, Any]:
    """The keyword arguments of a service task (``StandaloneJob`` + the stage's inputs)."""
    output_dir = storage.job_dir(item_dir, job)
    return {"run_id": str(run.id), "job": job_key(job), "token": token,
            "output_dir": output_dir, "outcome": f"{output_dir}/{OUTCOME_FILE}", **inputs}


def clean_inputs(pages: list[Page], storage: Storage) -> dict[str, Any]:
    """What ``evilflowers_scan.clean`` needs: the persisted leaf plan of each page + overrides."""
    leaves = []
    for p in pages:
        if not p.source_path or not p.crop_box:
            raise PortError(f"leaf {p.index} of book {p.book_pk} was never planned")
        leaves.append({"leaf": p.index, "frame": storage.rel(p.source_path), "side": p.side,
                       "crop_box": list(p.crop_box), "rotate_degree": p.rotate_degree or 0,
                       "hand_side": p.hand_side, "override": dict(p.override or {})})
    return {"leaves": leaves}


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


def discard(storage: Storage, job: Job) -> None:
    """Delete a refused attempt's output directory (never anything outside it)."""
    rel = (job.params or {}).get("output_dir")
    if not rel or storage.root is None:
        return
    try:
        shutil.rmtree(storage.abs(rel), ignore_errors=True)
    except OutcomeError:
        pass
