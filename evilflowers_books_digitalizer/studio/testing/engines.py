"""The engines inside the stand-in services (tests only).

Before evilflowers-books-digitalizer#6 these were the orchestrator's own ports (ScanTailor,
Tesseract, the layout detectors, PaddleOCR-VL, the renderer ran in-process). Now each lives in
its service; the reference tasks in :mod:`.standalone` drive these shapes so the tests can
stand in for a service with a deterministic fake (:mod:`.fakes`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from evilflowers_books_digitalizer.studio.domain import BBox, PageOverride, RegionProposal
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.ports import CleanedPage, LeafPlan


class PageCleaner(Protocol):
    """scan-service's cleaner: split, deskew, dewarp, crop, binarize."""

    def clean(self, plans: Sequence[LeafPlan], out_dir: Path,
              overrides: dict[int, PageOverride] | None = None) -> list[CleanedPage]: ...


class ImageOps(Protocol):
    """scan-service's pixel operations: erasures and the page's preview + tiles."""

    def erase(self, image: Path, boxes: Sequence[BBox], out: Path) -> Path: ...
    def previews(self, image: Path, out_dir: Path, *, max_px: int) -> Path: ...


@dataclass(frozen=True, slots=True)
class OcrPage:
    hocr: str                 # one ocr_page element (hOCR, UTF-8)
    text: str
    mean_conf: float          # 0..100 over words


class OcrEngine(Protocol):
    """ocr-service's engine (Tesseract)."""

    def recognize(self, image: Path, *, language: str, dpi: int) -> OcrPage: ...


class LayoutDetector(Protocol):
    """analyzer-service's engine (Docling): region proposals in page pixels."""

    name: str

    def detect(self, image: Path, *, ocr: OcrPage | None = None) -> list[RegionProposal]: ...


@dataclass(frozen=True, slots=True)
class FormulaJob:
    key: str
    image: Path
    bbox: BBox
    task: str                 # "formula" | "ocr"


class FormulaRecognizer(Protocol):
    """equation-service's engine (PaddleOCR-VL): ``{job.key: text}``, failed jobs absent."""

    def recognize(self, jobs: Sequence[FormulaJob]) -> dict[str, str]: ...


@dataclass(frozen=True, slots=True)
class RenderRequest:
    item_id: str
    pages: Sequence[Path]             # cleaned page images, leaf order, access pages only
    hocr: Path                        # multi-page hOCR in the same order
    metadata: ItemMetadata
    formats: Sequence[str]            # subset of {"pdf", "pdfa", "epub"}
    extra: dict[str, object] = field(default_factory=dict)


class Renderer(Protocol):
    """render-service's engine: ``{format: path}`` for every produced file."""

    def render(self, req: RenderRequest) -> dict[str, Path]: ...
