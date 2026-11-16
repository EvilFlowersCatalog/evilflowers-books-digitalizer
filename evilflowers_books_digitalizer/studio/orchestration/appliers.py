"""Service outcomes -> rows, per stage. Each reuses the in-process stage's own row logic.

"Promotion" of a job-scoped output is a pointer switch: the page's paths are set to the
files inside the attempt's ``output_dir``, in the same transaction that marks the job
applied. A refused attempt's directory is deleted (:func:`contract.discard`).
"""

from __future__ import annotations

from evilflowers_books_digitalizer.db.models import Job, Run
from evilflowers_books_digitalizer.studio.orchestration import contract, ledger
from evilflowers_books_digitalizer.studio.ports import CleanedPage
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import Uow, WorkerContext
from evilflowers_books_digitalizer.studio.workers.stages.clean import apply_cleaned


def apply_clean(uow: Uow, ctx: WorkerContext, run: Run, job: Job,
                outcome: contract.Outcome) -> None:
    assert isinstance(outcome, contract.CleanOutcome)
    storage = contract.Storage(ctx.config.storage_root)
    wanted = set(job.params.get("pages") or [])
    by_index = {p.index: p for p in run.book.pages}
    unknown = sorted({leaf.leaf for leaf in outcome.leaves} - wanted)
    if unknown:
        raise contract.OutcomeError(f"job {job.id}: outcome has leaves it was not given: {unknown}")
    for leaf in outcome.leaves:
        cleaned = CleanedPage(leaf.leaf, storage.abs(leaf.image), leaf.width, leaf.height,
                              leaf.dpi, leaf.skew_deg)
        webp = storage.abs(leaf.preview) if leaf.preview else storage.abs(leaf.image)
        apply_cleaned(uow, by_index[leaf.leaf], cleaned, storage.abs(leaf.dzi), webp)
    runs.advance(uow, run, len(outcome.leaves))


def install() -> None:
    """Register the appliers (idempotent; called when the worker tasks are imported)."""
    ledger.APPLIERS.setdefault("clean", apply_clean)
