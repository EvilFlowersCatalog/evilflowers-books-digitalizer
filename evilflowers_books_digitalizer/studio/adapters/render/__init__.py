"""Renderer over the classic pipeline steps: access leaves -> PDF, PDF/A-2b, EPUB.

* ``pdf``  — ``RenderPdf`` with the DISTRIBUTION profile (JPEG MRC, recode_pdf),
  then ``FinalizePdf`` (bookmarks, page labels);
* ``pdfa`` — the ARCHIVAL profile (JPEG2000 MRC) + ``EnsurePdfA`` (PDF/A-2b);
* ``epub`` — ``reflow.build_edition`` (born-digital reading edition), steered by
  the studio's region rows when the caller passes them (``extra["regions"]``, one
  list per access page): removed regions are dropped, ``as_image`` ones become
  crops, formulas use the LaTeX the formulas stage stored (no VLM here).

The request already carries *access* leaves only (the caller filters on IA's
``addToAccessFormats``), so the PDFs need no ``--scandata``: page N of every output
is access index N. ``RenderPdf`` insists that its page directory holds nothing
but the pages, so the leaves are staged (hard links) into a private directory.
Outputs go where the caller says (``extra["targets"]``: format -> path, from the
ArtifactStore); without it, IA names (``ia.naming``) in ``extra["out_dir"]``.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.pipeline.base import BookContext
from evilflowers_books_digitalizer.pipeline.profiles import ARCHIVAL, DISTRIBUTION, PdfProfile
from evilflowers_books_digitalizer.pipeline.steps.finalize import FinalizePdf
from evilflowers_books_digitalizer.pipeline.steps.pdfa import EnsurePdfA
from evilflowers_books_digitalizer.pipeline.steps.render import RenderPdf
from evilflowers_books_digitalizer.catalog.mapping import normalize_language
from evilflowers_books_digitalizer.reflow import ReflowSettings, build_edition
from evilflowers_books_digitalizer.reflow.edition import RegionDecision
from evilflowers_books_digitalizer.studio.adapters.ia.naming import ia_name
from evilflowers_books_digitalizer.studio.domain import RegionAction, RegionKind, RegionStatus
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.ports import PortError, RenderRequest

logger = logging.getLogger(__name__)

#: output format -> classic profile
PDF_FORMATS: dict[str, PdfProfile] = {"pdf": DISTRIBUTION, "pdfa": ARCHIVAL}


def target_path(req: RenderRequest, out_dir: Path, fmt: str) -> Path:
    """Where ``fmt`` goes: the caller's ``extra["targets"]`` (the ArtifactStore's
    path), else the IA name in ``out_dir`` — one spelling, from ``ia.naming``."""
    targets = req.extra.get("targets")
    if isinstance(targets, dict) and fmt in targets:
        return Path(str(targets[fmt]))
    return out_dir / ia_name(req.item_id, fmt)


def classic_metadata(meta: ItemMetadata) -> dict[str, Any]:
    """ItemMetadata -> the ``ctx.metadata`` keys the classic steps read.

    The language goes through the catalog's ``normalize_language`` so the PDF XMP,
    the EPUB ``dc:language`` and the catalog entry always agree.
    """
    lang = meta.language[0] if meta.language else ""
    return {"title": meta.title, "authors": list(meta.creator),
            "language": normalize_language(lang) or lang,
            "publisher": meta.publisher, "year": meta.date[:4] if meta.date else "",
            "isbn": meta.isbn[0] if meta.isbn else "", "description": " ".join(meta.description)}


def _decision(region: Any) -> RegionDecision | None:
    """One region row (``extra["regions"]`` dict) -> what the EPUB honours, or None.

    Confirmed regions always count. A detector's *proposed* region counts only when
    it changes the edition by itself — a removal, an image crop or a formula (the
    formulas stage stores its LaTeX there); proposed text labels would merely
    second-guess the heuristic analysis. Disputed and rejected regions are ignored.
    """
    if not isinstance(region, dict):
        return None
    status, kind, action = (str(region.get(k) or "") for k in ("status", "kind", "action"))
    if status == RegionStatus.PROPOSED:
        if not (action in (RegionAction.REMOVE, RegionAction.AS_IMAGE)
                or kind in (RegionKind.FORMULA, RegionKind.PICTURE)):
            return None
    elif status != RegionStatus.CONFIRMED:
        return None
    try:
        x0, y0, x1, y1 = (int(round(float(v))) for v in region["bbox"])
    except (KeyError, TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    text = region.get("text")
    latex = text if kind == RegionKind.FORMULA and isinstance(text, str) and text.strip() \
        else None
    return RegionDecision(kind, (x0, y0, x1, y1), action or str(RegionAction.KEEP), latex)


def region_decisions(regions: Any, n_pages: int) -> list[list[RegionDecision]] | None:
    """``extra["regions"]`` (one list of region dicts per access page, as the render
    stage passes them) -> per-page decisions; None (heuristics only) when absent."""
    if not isinstance(regions, list | tuple):
        return None
    if len(regions) != n_pages:
        raise PortError(f"{len(regions)} region lists for {n_pages} access pages")
    return [[d for r in (page or []) if (d := _decision(r)) is not None] for page in regions]


def stage_pages(pages: list[Path], dest: Path) -> list[Path]:
    """Hard-link (copy across devices) pages into ``dest`` as ``0000<ext>``, ``0001<ext>``…"""
    if not pages:
        raise PortError("nothing to render: the book has no access leaves")
    suffix = pages[0].suffix
    if any(p.suffix != suffix for p in pages):
        raise PortError("access leaves must share one image format")
    dest.mkdir(parents=True)
    staged = []
    for i, src in enumerate(pages):
        dst = dest / f"{i:04d}{suffix}"
        try:
            os.link(src.resolve(), dst)
        except OSError:
            shutil.copy2(src, dst)
        staged.append(dst)
    return staged


class StudioRenderer:
    def __init__(self, settings: dict[str, Any] | None = None, *, dpi: int = 600,
                 reflow: ReflowSettings | None = None, finalize: bool = True) -> None:
        self.settings = settings or {}       # the project's [pipeline] table
        self.dpi = dpi
        self.reflow = reflow or ReflowSettings()
        self.finalize = finalize

    def render(self, req: RenderRequest) -> dict[str, Path]:
        out_dir = Path(str(req.extra.get("out_dir") or req.hocr.parent)).resolve()
        work = out_dir / f".render-{req.item_id}"
        if work.exists():
            shutil.rmtree(work)
        try:
            pages = stage_pages([Path(p) for p in req.pages], work / "pages")
            meta = classic_metadata(req.metadata)
            produced = self._pdfs(req, pages, work, out_dir, meta)
            if "epub" in req.formats and (epub := self._epub(req, pages, work, out_dir, meta)):
                produced["epub"] = epub
            return produced
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _profiles(self, formats: list[str]) -> list[tuple[str, PdfProfile]]:
        overrides = self.settings.get("render", {})
        return [(fmt, PDF_FORMATS[fmt].with_overrides(overrides.get(PDF_FORMATS[fmt].name, {})))
                for fmt in formats if fmt in PDF_FORMATS]

    def _pdfs(self, req: RenderRequest, pages: list[Path], work: Path, out_dir: Path,
              meta: dict[str, Any]) -> dict[str, Path]:
        profiles = self._profiles(list(req.formats))
        if not profiles:
            return {}
        ctx = BookContext(source="studio", book_id=req.item_id, work_dir=work,
                          output_dir=work / "out", tiffs=pages,
                          artifacts={"hocr": req.hocr.resolve()}, metadata=dict(meta))
        try:
            RenderPdf([p for _, p in profiles], dpi=int(str(req.extra.get("dpi") or self.dpi))
                      ).run(ctx)
            if self.finalize:
                FinalizePdf(**self.settings.get("finalize", {})).run(ctx)
            EnsurePdfA().run(ctx)
        except (RuntimeError, ValueError) as exc:
            raise PortError(f"PDF rendering failed for {req.item_id}: {exc}") from exc
        produced = {}
        for fmt, profile in profiles:
            target = target_path(req, out_dir, fmt)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(ctx.artifacts[f"pdf_{profile.name}"], target)
            produced[fmt] = target
        return produced

    def _epub(self, req: RenderRequest, pages: list[Path], work: Path, out_dir: Path,
              meta: dict[str, Any]) -> Path | None:
        """The EPUB is a derived edition: a failure is logged, never fatal (as in the CLI)."""
        target = target_path(req, out_dir, "epub")
        try:
            build_edition(pages, req.hocr, target, work / "reflow", meta, self.reflow,
                          decisions=region_decisions(req.extra.get("regions"), len(pages)))
        except Exception as exc:  # noqa: BLE001 — reflow failures must not fail the book
            logger.error("%s: EPUB failed: %s", req.item_id, exc, exc_info=True)
            return None
        return target
