"""``studio.formulas``: re-read ``reocr`` regions (formulas, tables) with the recognizer.

The guards from ``reflow.vlm`` judge every recognizer's answer the same way: LaTeX
is normalised (``fix_latex``) and a "formula" that reads like a drawing is rejected
(``looks_hallucinated``). A rejection is written into ``regions.reason`` with the
:data:`REJECTED` prefix, which is what the quality gate counts; a region left
without text is a fallback (it stays an image crop in the outputs).
"""

from __future__ import annotations

from pathlib import Path

from evilflowers_books_digitalizer.db.models import Page, Region, Run
from evilflowers_books_digitalizer.reflow.vlm import fix_latex, looks_hallucinated
from evilflowers_books_digitalizer.studio.domain import RegionAction, RegionKind, RegionStatus
from evilflowers_books_digitalizer.studio.ports import FormulaJob
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import Uow, WorkerContext
from evilflowers_books_digitalizer.studio.workers.stages import common

REJECTED = "vlm rejected"
FAILED = "vlm gave no answer: kept as image"
#: detector-owned statuses; confirmed regions keep the operator's text
OPEN = (RegionStatus.PROPOSED, RegionStatus.DISPUTED)


def job_key(region_id: int) -> str:
    return f"region-{region_id}"


def region_id(key: str) -> int:
    return int(key.removeprefix("region-"))


def to_read(page: Page) -> list[Region]:
    """The regions of a page the recognizer must (re-)read: ``reocr`` ones no person decided."""
    if not page.clean_path:
        return []
    return [r for r in page.regions if r.action == RegionAction.REOCR and r.status in OPEN]


def task_of(region: Region) -> str:
    """``formula`` (math only) or ``ocr`` (a table / a mixed math + prose line)."""
    return "formula" if region.kind == RegionKind.FORMULA else "ocr"


def _jobs(ctx: WorkerContext, run_id: int) -> tuple[str, list[FormulaJob]]:
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        book = run.book
        jobs = [FormulaJob(job_key(r.id), Path(p.clean_path or ""), common.region_bbox(r),
                           task_of(r))
                for p in common.selected(book.pages, run.pages) for r in to_read(p)]  # to_read: cleaned
        runs.enter_stage(uow, run, "formulas", len(jobs))
        return runs.project_of(book), jobs


def judge(answer: str | None) -> tuple[str | None, str | None]:
    """Recognizer answer -> ``(text, reason)``; a rejected answer keeps no text."""
    if not answer or not answer.strip():
        return None, FAILED
    tex = fix_latex(answer)
    if looks_hallucinated(tex):
        return None, f"{REJECTED}: looks like a drawing"
    return tex, None


def merge_reason(current: str | None, new: str | None) -> str | None:
    """Keep a dispute reason, replace any earlier recognizer verdict (idempotent re-runs)."""
    parts = [p for p in (current or "").split("; ") if p and not p.startswith("vlm ")]
    return "; ".join([*parts, new] if new else parts) or None


def apply_answers(uow: Uow, run: Run, keys: list[str], answers: dict[str, str]) -> None:
    """The recognizer's answers for ``keys`` into the regions (in-process and service alike).

    A missing answer is a failure (the region stays an image crop); every answer passes
    :func:`judge` here, whoever recognized it, so the gate counts rejections the same way.
    """
    rejected = 0
    for key in keys:
        # row lock: an operator PATCH waits for this short commit instead of racing it
        region = uow.session.get(Region, region_id(key), with_for_update=True)
        if region is None or region.status not in OPEN:
            continue            # the operator decided it while the recognizer ran
        text, reason = judge(answers.get(key))
        rejected += bool(reason and reason.startswith(REJECTED))
        if text is not None:
            region.text = text
        elif region.kind == RegionKind.FORMULA:
            region.text = None    # Tesseract's guess at math is noise
        region.reason = merge_reason(region.reason, reason)
    runs.log(uow, run, f"formulas: {len(keys)} jobs, {len(answers)} answered, "
                       f"{rejected} rejected")


def formulas(ctx: WorkerContext, run_id: int) -> None:
    project, jobs = _jobs(ctx, run_id)
    if not jobs:
        return
    answers = ctx.adapters(project).formulas.recognize(jobs)
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        apply_answers(uow, run, [job.key for job in jobs], answers)
        runs.advance(uow, run, len(jobs))
