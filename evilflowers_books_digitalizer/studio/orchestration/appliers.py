"""Service outcomes -> rows, per job kind. Each reuses the in-process stage's own row logic.

"Promotion" of a job-scoped output is a pointer switch: the page's paths are set to the
files inside the attempt's ``output_dir``, in the same transaction that marks the job
applied. Nothing is copied, so applying is cheap and atomic with the rows.

An applier returns the attempt dirs its pages pointed into *before*; the ledger deletes
those after the commit once no page points into them (a re-run superseded them). The
dirs of refused, failed and revoked attempts are deleted by the ledger as well.

Analyze runs as two parts per leaf batch (docs/diagrams/sequence-digitalizer-analyze-service):
``hocr`` and ``layout`` share the batch's ``seq``; whichever is applied second fuses the
batch with the operator's decisions (``stages.analyze``: the in-process code). A leaf with
confirmed "remove" boxes then gets an ``erase`` job on scan-service: the orchestrator never
changes a page's pixels itself.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path

from evilflowers_books_digitalizer.db.models import Job, Page, Run
from evilflowers_books_digitalizer.studio.config import SPLIT_STAGES
from evilflowers_books_digitalizer.studio.domain import (
    BBox,
    JobStatus,
    RegionKind,
    RegionProposal,
    RegionSource,
)
from evilflowers_books_digitalizer.studio.orchestration import contract, ledger
from evilflowers_books_digitalizer.studio.ports import CleanedPage
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import Uow, WorkerContext
from evilflowers_books_digitalizer.studio.workers.stages import analyze, formulas, render
from evilflowers_books_digitalizer.studio.workers.stages.clean import apply_cleaned, replace_image

logger = logging.getLogger(__name__)

#: Docling labels (evilflowers-analyzer-service) -> region kinds; DocLayNet names map to themselves
DOCLING_KINDS: dict[str, RegionKind] = {
    "figure": RegionKind.PICTURE, "chart": RegionKind.PICTURE, "picture": RegionKind.PICTURE,
    "equation": RegionKind.FORMULA, "paragraph": RegionKind.TEXT,
    "handwritten_text": RegionKind.HANDWRITING,
}

# ---------------------------------------------------------------- helpers
def _pages(job: Job, run: Run, leaves: list[int]) -> dict[int, Page]:
    """The pages of ``leaves``; every one must be a leaf the job was given."""
    unknown = sorted(set(leaves) - set(job.params.get("pages") or []))
    if unknown:
        raise contract.OutcomeError(f"job {job.id}: outcome has leaves it was not given: {unknown}")
    by_index = {p.index: p for p in run.book.pages}
    return {leaf: by_index[leaf] for leaf in leaves}


def _previous(page: Page, mine: Path, *paths: str | None) -> set[Path]:
    return {d for d in (contract.attempt_dir(p) for p in paths) if d is not None and d != mine}


# ---------------------------------------------------------------- clean
def apply_clean(uow: Uow, ctx: WorkerContext, run: Run, job: Job,
                outcome: contract.Outcome) -> list[Path]:
    assert isinstance(outcome, contract.CleanOutcome)
    storage = contract.Storage(ctx.config.storage_root)
    pages = _pages(job, run, [leaf.leaf for leaf in outcome.leaves])
    mine = storage.abs(job.params["output_dir"])
    previous: set[Path] = set()
    for leaf in outcome.leaves:
        page = pages[leaf.leaf]
        previous |= _previous(page, mine, page.clean_path, page.dzi_path, page.preview_path)
        cleaned = CleanedPage(leaf.leaf, storage.abs(leaf.image), leaf.width, leaf.height,
                              leaf.dpi, leaf.skew_deg)
        webp = storage.abs(leaf.preview) if leaf.preview else storage.abs(leaf.image)
        apply_cleaned(uow, page, cleaned, storage.abs(leaf.dzi), webp)
        if leaf.crop_box is not None:    # what was cut (clamped, split_x, override crop)
            page.crop_box = list(leaf.crop_box)
        page.marks = [m.model_dump(mode="json") for m in leaf.marks]
        for warning in leaf.warnings:
            runs.log(uow, run, f"leaf {leaf.leaf}: {warning}", "warning")
    runs.advance(uow, run, len(outcome.leaves))
    return sorted(previous)


# ---------------------------------------------------------------- analyze: hocr ∥ layout
def apply_hocr(uow: Uow, ctx: WorkerContext, run: Run, job: Job,
               outcome: contract.Outcome) -> list[Path]:
    assert isinstance(outcome, contract.HocrOutcome)
    _pages(job, run, [leaf.leaf for leaf in outcome.leaves])
    return _fuse_when_complete(uow, ctx, run, job)


def apply_layout(uow: Uow, ctx: WorkerContext, run: Run, job: Job,
                 outcome: contract.Outcome) -> list[Path]:
    assert isinstance(outcome, contract.LayoutOutcome)
    if outcome.regions is None:
        raise contract.OutcomeError(f"job {job.id}: a done layout outcome needs regions")
    _pages(job, run, [r.leaf for r in _regions(ctx, job, outcome)] + list(outcome.leaves))
    return _fuse_when_complete(uow, ctx, run, job)


def _regions(ctx: WorkerContext, job: Job, outcome: contract.LayoutOutcome
             ) -> list[contract.LayoutRegion]:
    storage = contract.Storage(ctx.config.storage_root)
    try:
        rows = json.loads(storage.abs(outcome.regions or "").read_text(encoding="utf-8"))
        return [contract.LayoutRegion.model_validate(r) for r in rows]
    except (OSError, ValueError) as exc:
        raise contract.OutcomeError(f"job {job.id}: unreadable regions.json: {exc}") from exc


def _batch(run: Run, job: Job) -> dict[str, Job]:
    """The batch's parts that are sent together (``hocr``, ``layout``), by part."""
    parts = SPLIT_STAGES[job.stage]
    return {p: j for j in run.jobs
            if j.stage == job.stage and j.seq == job.seq and (p := contract.part(j)) in parts}


def _fuse_when_complete(uow: Uow, ctx: WorkerContext, run: Run, job: Job) -> list[Path]:
    """Fuse the batch once every part is in (``job`` is being applied right now)."""
    batch = _batch(run, job)
    if any(j.id != job.id and j.status != JobStatus.APPLIED for j in batch.values()):
        return []                        # the other half is still out: its turn fuses
    return fuse_batch(uow, ctx, run, batch["hocr"], batch["layout"])


def _proposal(region: contract.LayoutRegion, page: Page) -> RegionProposal | None:
    kind = DOCLING_KINDS.get(region.kind)
    if kind is None:
        try:
            kind = RegionKind(region.kind)
        except ValueError:
            logger.info("layout kind %r ignored (no region kind)", region.kind)
            return None
    w, h = page.width or 0, page.height or 0
    x0, y0, x1, y1 = (min(max(v, 0.0), 1.0) for v in region.bbox)
    try:
        box = BBox.of((x0 * w, y0 * h, x1 * w, y1 * h))
    except ValueError:
        return None                      # a degenerate box (zero area after rounding)
    return RegionProposal(kind, box, RegionSource.DOCLING, region.confidence)


def fuse_batch(uow: Uow, ctx: WorkerContext, run: Run, hocr_job: Job,
               layout_job: Job) -> list[Path]:
    """Both halves of a batch -> regions, OCR text and hOCR per leaf (the in-process store)."""
    storage = contract.Storage(ctx.config.storage_root)
    hocr = contract.read_outcome(storage, hocr_job, contract.HocrOutcome)
    layout = contract.read_outcome(storage, layout_job, contract.LayoutOutcome)
    assert isinstance(hocr, contract.HocrOutcome) and isinstance(layout, contract.LayoutOutcome)
    by_leaf: dict[int, list[contract.LayoutRegion]] = defaultdict(list)
    if layout.status == "done":
        for region in _regions(ctx, layout_job, layout):
            by_leaf[region.leaf].append(region)
    pages = _pages(hocr_job, run, [leaf.leaf for leaf in hocr.leaves])
    mine = storage.abs(hocr_job.params["output_dir"])
    previous: set[Path] = set()
    erase: list[int] = []
    for leaf in hocr.leaves:
        page = pages[leaf.leaf]
        previous |= _previous(page, mine, page.hocr_path)
        proposals = [p for r in by_leaf.get(leaf.leaf, []) if (p := _proposal(r, page))]
        proposals += analyze.mark_proposals(page)        # scan-service's stamps, stickers
        analyze.store(uow, run, page, text=leaf.text, mean_conf=leaf.mean_conf,
                      hocr_path=storage.abs(leaf.hocr),
                      fused=analyze.fuse_with(analyze.decided_of(page), proposals))
        if analyze.removed_boxes(page):
            erase.append(page.index)
    runs.advance(uow, run, len(hocr.leaves))
    if erase:
        ledger.add_job(uow, ctx, run, hocr_job.stage, hocr_job.seq,
                       {"pages": erase, "part": "erase"})
        runs.log(uow, run, f"analyze batch {hocr_job.seq}: erase confirmed boxes on "
                           f"{len(erase)} leaf(s)")
    # the layout attempt is consumed: nothing points into it (deleted after the commit)
    return sorted(previous | {storage.abs(layout_job.params["output_dir"])})


# ---------------------------------------------------------------- analyze: erase
def apply_erase(uow: Uow, ctx: WorkerContext, run: Run, job: Job,
                outcome: contract.Outcome) -> list[Path]:
    """The page image with its confirmed "remove" boxes painted out: a pointer switch."""
    assert isinstance(outcome, contract.CleanOutcome)
    storage = contract.Storage(ctx.config.storage_root)
    pages = _pages(job, run, [leaf.leaf for leaf in outcome.leaves])
    mine = storage.abs(job.params["output_dir"])
    previous: set[Path] = set()
    for leaf in outcome.leaves:
        page = pages[leaf.leaf]
        if (leaf.width, leaf.height) != (page.width, page.height):
            raise contract.OutcomeError(f"job {job.id}: erase changed leaf {leaf.leaf}'s size")
        previous |= _previous(page, mine, page.clean_path, page.dzi_path, page.preview_path)
        webp = storage.abs(leaf.preview) if leaf.preview else storage.abs(leaf.image)
        replace_image(uow, page, storage.abs(leaf.image), storage.abs(leaf.dzi), webp)
    return sorted(previous)


# ---------------------------------------------------------------- formulas
def apply_formulas(uow: Uow, ctx: WorkerContext, run: Run, job: Job,
                   outcome: contract.Outcome) -> list[Path]:
    """The recognizer's answers into the regions it was asked about; the in-process guards
    (``fix_latex``, ``looks_hallucinated``) judge them, so the gate counts the same way."""
    assert isinstance(outcome, contract.FormulasOutcome)
    if outcome.formulas is None:
        raise contract.OutcomeError(f"job {job.id}: a done formulas outcome needs formulas")
    storage = contract.Storage(ctx.config.storage_root)
    try:
        rows = json.loads(storage.abs(outcome.formulas).read_text(encoding="utf-8"))
        answers = [contract.FormulaAnswer.model_validate(r) for r in rows]
    except (OSError, ValueError) as exc:
        raise contract.OutcomeError(f"job {job.id}: unreadable formulas.json: {exc}") from exc
    asked = list(job.params.get("regions") or [])
    unknown = sorted({a.key for a in answers} - set(asked))
    if unknown:
        raise contract.OutcomeError(f"job {job.id}: answers for regions it was not asked: "
                                    f"{unknown[:5]}")
    formulas.apply_answers(uow, run, asked, {a.key: a.text for a in answers if a.text})
    runs.advance(uow, run, len(job.params.get("pages") or []))
    # the answers now live in the rows: the attempt dir is consumed (deleted after the commit)
    return [storage.abs(job.params["output_dir"])]


# ---------------------------------------------------------------- render: book
def apply_render_book(uow: Uow, ctx: WorkerContext, run: Run, job: Job,
                      outcome: contract.Outcome) -> list[Path]:
    """The media move into the IA item layout (an atomic rename on the same storage, never a
    copy); the rows and the IA documents follow through the in-process tail (``finish``)."""
    assert isinstance(outcome, contract.RenderOutcome)
    storage = contract.Storage(ctx.config.storage_root)
    mine = storage.abs(job.params["output_dir"])
    request = render.read_request(storage.abs(job.params["request"]))
    unknown = sorted(set(outcome.files) - set(render.SERVICE_MEDIA))
    missing = sorted(set(request["formats"]) - set(outcome.files))
    if unknown or missing:
        raise contract.OutcomeError(f"job {job.id}: render files unknown {unknown}, "
                                    f"missing {missing}")
    if outcome.pdfa_valid is False:
        raise contract.OutcomeError(f"job {job.id}: the PDF/A master failed validation")
    sources = {}
    for kind, produced in outcome.files.items():
        src = storage.abs(produced.path)
        if not src.is_relative_to(mine) or not src.is_file() or src.stat().st_size != produced.size:
            raise contract.OutcomeError(f"job {job.id}: {kind} file {produced.path} is not the "
                                        "attempt's, missing or of another size")
        sources[kind] = src
    store = ctx.adapters(runs.project_of(run.book)).artifacts
    placed = {}
    for kind, src in sources.items():           # every check passed: now move them all
        dst = store.path(request["item_id"], kind)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dst)
        placed[kind] = dst
    render.finish(uow, ctx, run, placed, list(request["leaves"]), int(request["n_leaves"]))
    return [mine]                               # what is left in it: the outcome file


def install() -> None:
    """Register the appliers (idempotent; called when the worker tasks are imported)."""
    for key, applier in {"clean": apply_clean, "analyze.hocr": apply_hocr,
                         "analyze.layout": apply_layout, "analyze.erase": apply_erase,
                         "formulas": apply_formulas, "render.book": apply_render_book}.items():
        ledger.APPLIERS.setdefault(key, applier)
