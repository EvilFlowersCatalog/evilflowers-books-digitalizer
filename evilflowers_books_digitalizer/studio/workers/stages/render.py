"""``studio.render``: metadata lookup + IA item files + Renderer outputs (access leaves only).

Metadata is looked up here, after OCR, because two providers need the text
(ISBN from the colophon). Operator edits win: the stored record is merged *over*
the lookup (``ItemMetadata.merge`` fills gaps only).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from evilflowers_books_digitalizer.db.models import Book, Page
from evilflowers_books_digitalizer.studio.adapters.ia import IaItemExporter
from evilflowers_books_digitalizer.studio.application.pages import PDF_PAGES_STAT
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.ports import PortError, RenderRequest, ScannedBook
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import WorkerContext
from evilflowers_books_digitalizer.studio.adapters.registry import output_dpi
from evilflowers_books_digitalizer.studio.workers.stages import common, item_files
from evilflowers_books_digitalizer.studio.workers.stages.item_files import LeafFiles

#: Renderer formats the studio knows (``ProjectConfig.outputs``)
FORMATS = ("pdf", "pdfa", "epub")


@dataclass(frozen=True, slots=True)
class RenderJob:
    project: str
    item_id: str
    book: ScannedBook
    metadata: ItemMetadata
    leaves: list[LeafFiles]


def _leaf(page: Page) -> LeafFiles:
    regions = [{"id": r.id, "kind": r.kind, "bbox": [r.x0, r.y0, r.x1, r.y1], "status": r.status,
                "action": r.action, "source": r.source, "text": r.text} for r in page.regions]
    return LeafFiles(page.index, page.add_to_access,
                     Path(page.clean_path) if page.clean_path else None,
                     Path(page.hocr_path) if page.hocr_path else None,
                     page.width or 0, page.height or 0, page.ocr_text or "", regions)


def _job(ctx: WorkerContext, run_id: int) -> RenderJob | None:
    """What to render; ``None`` when a previous attempt already committed the outputs."""
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        book = run.book
        runs.enter_stage(uow, run, "render", 1)
        if runs.stage_done(run):
            return None
        if not book.pages:
            raise PortError(f"book {book.item_id} has no leaves: run a full run first")
        return RenderJob(runs.project_of(book), common.item_id(book), common.scanned_book(book),
                         common.book_metadata(book), [_leaf(p) for p in book.pages])


def complete_metadata(ctx: WorkerContext, job: RenderJob) -> ItemMetadata:
    project = ctx.project(job.project)
    found = ctx.adapters(job.project).metadata.lookup(
        job.book, ocr_text="\f".join(lf.text for lf in job.leaves))
    meta = ItemMetadata.from_dict(job.metadata.as_dict()).merge(found)
    meta.imagecount = len(job.leaves)
    meta.ppi = meta.ppi or output_dpi(project)
    meta.ocr = meta.ocr or "tesseract"
    meta.ocr_parameters = meta.ocr_parameters or f"-l {project.ocr_language}"
    return meta


def _place(produced: dict[str, Path], ctx: WorkerContext, job: RenderJob) -> dict[str, Path]:
    """Move renderer outputs to their artifact-store names (a renderer that ignores
    ``extra["targets"]``, e.g. a test fake; a no-op for the studio renderer)."""
    store = ctx.adapters(job.project).artifacts
    placed = {}
    for fmt, src in produced.items():
        dst = store.path(job.item_id, fmt)
        if src.resolve() != dst.resolve():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dst)
        placed[fmt] = dst
    return placed


def check_access_leaves(item: str, access: list[LeafFiles]) -> None:
    """Refuse to render rather than ship a PDF without pages or without a text layer.

    Cleaned pages and per-leaf hOCR live in ``cache_dir`` (scratch); after an
    eviction a render-only run must fail loudly and ask for a re-run.
    """
    missing_image = [lf.leaf for lf in access if lf.image is None or not lf.image.exists()]
    missing_hocr = [lf.leaf for lf in access if lf.hocr is None or not lf.hocr.exists()]
    if missing_image or missing_hocr:
        raise PortError(f"{item}: access leaves without cleaned image {missing_image[:10]} or "
                        f"OCR {missing_hocr[:10]} — run a full run (or re-run those pages)")


def write_item(ctx: WorkerContext, job: RenderJob, meta: ItemMetadata) -> dict[str, Path]:
    store = ctx.adapters(job.project).artifacts
    item = job.item_id
    access = [lf for lf in job.leaves if lf.in_access]
    check_access_leaves(item, access)          # before any artifact is overwritten
    files = {
        "hocr": item_files.write_hocr(job.leaves, store.path(item, "hocr"), access_only=False,
                                      title=meta.title),
        "text": item_files.write_text(job.leaves, store.path(item, "text")),
        "regions_json": item_files.write_regions(job.leaves, store.path(item, "regions_json")),
    }
    if ctx.project(job.project).pipeline.get("jp2_images", True):
        package = item_files.write_jp2_zip(job.leaves, store.path(item, "images"), item,
                                           cache_dir=ctx.work_dir(job.project, item) / "jp2")
        if package is not None:
            files["images"] = package
    hocr = item_files.write_hocr(job.leaves, ctx.work_dir(job.project, item) / "access_hocr.html",
                                 access_only=True, title=meta.title)
    formats = [f for f in ctx.project(job.project).outputs if f in FORMATS]
    # check_access_leaves guarantees an image per access leaf: pages[i] <-> regions[i]
    request = RenderRequest(item, [lf.image for lf in access if lf.image], hocr, meta, formats,
                            {"out_dir": store.item_dir(item),
                             "targets": {fmt: store.path(item, fmt) for fmt in formats},
                             "dpi": output_dpi(ctx.project(job.project)),
                             # the operator's region decisions steer the EPUB
                             "regions": [lf.regions for lf in access]})
    produced = ctx.adapters(job.project).renderer.render(request)
    return {**files, **_place(produced, ctx, job)}


def render(ctx: WorkerContext, run_id: int) -> None:
    job = _job(ctx, run_id)
    if job is None:
        return
    meta = complete_metadata(ctx, job)
    files = write_item(ctx, job, meta)
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        book: Book = run.book
        book.item_metadata = meta.as_dict()
        book.title = meta.title or book.title
        book.artifacts = {**(book.artifacts or {}), **{k: str(v) for k, v in files.items()}}
        book.output_dir = str(ctx.adapters(job.project).artifacts.item_dir(job.item_id))
        book.n_pages = len(job.leaves)
        # the viewer's pdf layer must map leaves the way *this* PDF was built
        book.stats = {**(book.stats or {}), PDF_PAGES_STAT: {
            str(leaf): i for i, leaf in enumerate(lf.leaf for lf in job.leaves if lf.in_access)}}
        # last, so _files.xml lists every file written above (IA documents from the rows)
        ia_docs = IaItemExporter(ctx.adapters(job.project).artifacts).write_item(book, book.pages)
        book.artifacts = {**book.artifacts, **{k: str(v) for k, v in ia_docs.items()}}
        runs.advance(uow, run)
        runs.log(uow, run, "rendered " + ", ".join(sorted({*files, *ia_docs})))
