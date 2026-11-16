"""Stage render, the orchestrator's half: ``prepare`` before render-service, ``finish`` after.

* ``render/prepare`` (in-process, :func:`prepare`): metadata lookup, the item's text documents
  (whole-book hOCR, plain text, regions) and the request render-service renders from;
* ``render/book`` (``evilflowers_render.book``): the media (PDF, PDF/A-2b, EPUB, JP2 package);
* applying ``book`` moves the media into the IA item layout and calls :func:`finish`
  (the rows and the IA XML documents).

Metadata is looked up here, after OCR, because two providers need the text
(ISBN from the colophon). Operator edits win: the stored record is merged *over*
the lookup (``ItemMetadata.merge`` fills gaps only).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.db.models import Book, Page, Run
from evilflowers_books_digitalizer.studio.adapters.ia import IaItemExporter
from evilflowers_books_digitalizer.studio.adapters.registry import output_dpi
from evilflowers_books_digitalizer.studio.application.pages import PDF_PAGES_STAT
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.orchestration.contract import Storage
from evilflowers_books_digitalizer.studio.ports import PortError, ScannedBook
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import Uow, WorkerContext
from evilflowers_books_digitalizer.studio.workers.stages import common, item_files
from evilflowers_books_digitalizer.studio.workers.stages.item_files import LeafFiles

#: formats render-service makes (``ProjectConfig.outputs``)
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


def text_files(ctx: WorkerContext, job: RenderJob, meta: ItemMetadata) -> dict[str, Path]:
    """The item's text documents (whole-book hOCR, plain text, regions): no pixels, so they
    stay with the orchestrator whoever renders the media."""
    store = ctx.adapters(job.project).artifacts
    item = job.item_id
    check_access_leaves(item, [lf for lf in job.leaves if lf.in_access])  # before overwriting
    return {
        "hocr": item_files.write_hocr(job.leaves, store.path(item, "hocr"), access_only=False,
                                      title=meta.title),
        "text": item_files.write_text(job.leaves, store.path(item, "text")),
        "regions_json": item_files.write_regions(job.leaves, store.path(item, "regions_json")),
    }


def _store_metadata(book: Book, meta: ItemMetadata) -> None:
    book.item_metadata = meta.as_dict()
    book.title = meta.title or book.title


def finish(uow: Uow, ctx: WorkerContext, run: Run, files: dict[str, Path],
           access_leaves: list[int], n_leaves: int) -> None:
    """The rows and the IA documents once render-service's media are in the item."""
    book: Book = run.book
    project, item = runs.project_of(book), common.item_id(book)
    store = ctx.adapters(project).artifacts
    book.artifacts = {**(book.artifacts or {}), **{k: str(v) for k, v in files.items()}}
    book.output_dir = str(store.item_dir(item))
    book.n_pages = n_leaves
    # the viewer's pdf layer must map leaves the way *this* PDF was built
    book.stats = {**(book.stats or {}),
                  PDF_PAGES_STAT: {str(leaf): i for i, leaf in enumerate(access_leaves)}}
    # last, so _files.xml lists every file written above (IA documents from the rows)
    ia_docs = IaItemExporter(store).write_item(book, book.pages)
    book.artifacts = {**book.artifacts, **{k: str(v) for k, v in ia_docs.items()}}
    runs.advance(uow, run)
    runs.log(uow, run, "rendered " + ", ".join(sorted({*book.artifacts})))


# ---------------------------------------------------------------- render on a service
#: the media a render service produces (artifact kinds); the text documents stay here
SERVICE_MEDIA = ("pdf", "pdfa", "epub", "images")
#: the item ids evilflowers-render-service accepts (its ``RenderRequest.item_id``)
ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def request_path(item_dir: Path, run_id: int) -> Path:
    """Where ``prepare`` leaves the render request of a run (on the shared storage)."""
    return item_dir / f"{item_dir.name}_studio" / "render" / f"request-{run_id}.json"


def prepare(ctx: WorkerContext, run_id: int) -> None:
    """``render/prepare`` (in-process, before ``evilflowers_render.book``): metadata lookup,
    the item's text documents, and the request the service renders from.

    Redis carries only the request's path; the request carries every path relative to the
    shared storage root, so the service may mount it anywhere (its ``STORAGE_PATH``).
    """
    job = _job(ctx, run_id)
    if job is None:
        return
    storage = Storage(ctx.config.storage_root)
    meta = complete_metadata(ctx, job)
    files = text_files(ctx, job, meta)
    store = ctx.adapters(job.project).artifacts
    target = request_path(store.item_dir(job.item_id), run_id)
    if target.parent.is_dir():     # an earlier run's request (one render per book at a time)
        for old in [*target.parent.glob("request-*.json"), *target.parent.glob("access_hocr-*")]:
            if not old.name.endswith((f"-{run_id}.json", f"-{run_id}.html")):
                old.unlink(missing_ok=True)
    access = [lf for lf in job.leaves if lf.in_access]
    hocr = item_files.write_hocr(job.leaves, target.parent / f"access_hocr-{run_id}.html",
                                 access_only=True, title=meta.title)
    project = ctx.project(job.project)
    images = [lf.image for lf in sorted(job.leaves, key=lambda lf: lf.leaf)]
    whole = project.pipeline.get("jp2_images", True) and all(i and i.exists() for i in images)
    # render-service refuses an empty or repeated format list and an id outside IA's pattern
    formats = list(dict.fromkeys(f for f in project.outputs if f in FORMATS))
    if not formats:
        raise PortError(f"project {job.project}: outputs {list(project.outputs)} name no "
                        f"format render-service makes ({', '.join(FORMATS)})")
    if not ITEM_ID.fullmatch(job.item_id):
        raise PortError(f"item id {job.item_id!r} is not an IA identifier ({ITEM_ID.pattern})")
    request = {
        "item_id": job.item_id,
        "formats": formats,
        "leaves": [lf.leaf for lf in access],
        "n_leaves": len(job.leaves),
        "pages": [storage.rel(lf.image) for lf in access if lf.image],
        "hocr": storage.rel(hocr),
        "metadata": meta.as_dict(),
        "dpi": output_dpi(project),
        # the operator's region decisions steer the EPUB (pages[i] <-> regions[i])
        "regions": [lf.regions for lf in access],
        # IA's page package must hold every leaf, cleaned: otherwise none is made
        "jp2": [storage.rel(i) for i in images if i] if whole else [],
    }
    tmp = target.with_name(target.name + ".tmp")     # atomic: a reader sees all or nothing
    tmp.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        _store_metadata(run.book, meta)
        run.book.artifacts = {**(run.book.artifacts or {}),
                              **{k: str(v) for k, v in files.items()}}
        runs.log(uow, run, f"render request for {len(access)} access leaves: {target.name}")


def read_request(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))
