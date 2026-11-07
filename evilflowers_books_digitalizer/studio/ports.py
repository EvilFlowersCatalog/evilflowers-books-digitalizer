"""Ports: the only things use cases may depend on (dependency rule).

Adapters in ``studio/adapters/`` implement these Protocols; a *project* picks
adapters by name in its config (``studio.config``). Nothing here imports an
adapter, a framework, or does I/O.

Conventions
-----------
* Paths are ``pathlib.Path``; the scan tree is read-only, the artifact tree is
  the only place adapters write durable files.
* Pixel coordinates are in the space named in each signature (frame = original
  TIFF, page = cleaned page image).
* Errors: adapters raise ``PortError`` subclasses for expected failures
  (missing file, tool exit code, remote 4xx); anything else is a bug.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from evilflowers_books_digitalizer.studio.domain import BBox, PageOverride, RegionProposal
from evilflowers_books_digitalizer.studio.events import Event
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata, Leaf


class PortError(RuntimeError):
    """Expected, reportable failure of an adapter."""


# ---------------------------------------------------------------- scans (read-only)
@dataclass(frozen=True, slots=True)
class ScannedBook:
    collection: str            # e.g. faculty key "fei" (first-level grouping of a project)
    external_id: str           # book directory name, stable across runs
    path: Path                 # directory holding the frames
    frames: tuple[Path, ...]   # page scans in capture order


@runtime_checkable
class ScanStore(Protocol):
    """A project's raw scan tree (TIFF frames per book)."""

    def collections(self) -> list[str]: ...
    def books(self, collection: str) -> Iterator[ScannedBook]: ...
    def book(self, collection: str, external_id: str) -> ScannedBook: ...


# ---------------------------------------------------------------- outputs (read-write)
#: every artifact kind; IA-named files per docs/design/studio-spec.md ("Output tree")
IA_KINDS = ("meta_xml", "scandata_xml", "images", "hocr", "page_numbers", "pdf", "pdfa", "epub",
            "text", "files_xml")
#: studio-only working files (per leaf: leaf_webp, dzi; per book: regions_json)
STUDIO_KINDS = ("leaf_webp", "dzi", "regions_json")
ARTIFACT_KINDS = IA_KINDS + STUDIO_KINDS


@runtime_checkable
class ArtifactStore(Protocol):
    """Where a book's derived files live (layout decided by the adapter, IA-compatible)."""

    def item_dir(self, item_id: str) -> Path: ...
    def path(self, item_id: str, kind: str, *, leaf: int | None = None) -> Path:
        """``kind`` is one of ARTIFACT_KINDS; ``leaf`` for per-page files."""
        ...
    def exists(self, item_id: str, kind: str, *, leaf: int | None = None) -> bool: ...


# ---------------------------------------------------------------- metadata
@runtime_checkable
class MetadataProvider(Protocol):
    """Best-known bibliographic metadata for a scanned book (Excel, OPAC, OCR …)."""

    def lookup(self, book: ScannedBook, *, ocr_text: str | None = None) -> ItemMetadata: ...


# ---------------------------------------------------------------- imaging
@dataclass(frozen=True, slots=True)
class LeafPlan:
    """Where one leaf comes from — decided ONCE per full run, before any cleaning.

    The first ``studio.clean`` of a full run calls :meth:`PageCleaner.plan` and
    persists one ``pages`` row per plan (leaf_num = index, crop_box, hand_side,
    rotate_degree, source_path). Every later clean (chunks, re-runs) works from the
    persisted rows, so leaf numbers are stable and overrides can be keyed by them.
    """

    leaf_num: int
    frame: Path
    side: int | None          # 0 left / 1 right half of a spread, None = whole frame
    crop: BBox                # frame pixels AFTER rotation (rotate_degree applied first);
                              # stored as IA x,y,w,h via BBox.to_xywh()
    rotate_degree: int = 0
    hand_side: str | None = None   # metadata.HandSide value


@dataclass(frozen=True, slots=True)
class CleanedPage:
    leaf_num: int
    image: Path               # cleaned page image (600 dpi bitonal/mixed TIFF)
    width: int
    height: int
    dpi: int
    skew_deg: float = 0.0


@runtime_checkable
class PageCleaner(Protocol):
    """Split, deskew, dewarp, crop, binarize (ScanTailor + OpenCV)."""

    def plan(self, frames: Sequence[Path]) -> list[LeafPlan]:
        """Spread detection -> leaves in capture order (leaf_num 0..n-1)."""
        ...

    def clean(self, plans: Sequence[LeafPlan], out_dir: Path,
              overrides: dict[int, PageOverride] | None = None) -> list[CleanedPage]:
        """Clean the given leaves; ``overrides`` keyed by ``leaf_num``."""
        ...


@runtime_checkable
class ImageOps(Protocol):
    """Pixel operations the operator can ask for (restoration)."""

    def erase(self, image: Path, boxes: Sequence[BBox], out: Path) -> Path: ...
    def previews(self, image: Path, out_dir: Path, *, max_px: int) -> Path:
        """Write a browser preview + deep-zoom tiles (DZI) for one page; returns the .dzi."""
        ...


# ---------------------------------------------------------------- recognition
@dataclass(frozen=True, slots=True)
class OcrPage:
    hocr: str                 # one ocr_page element (hOCR, UTF-8)
    text: str
    mean_conf: float          # 0..100 over words


@runtime_checkable
class OcrEngine(Protocol):
    def recognize(self, image: Path, *, language: str, dpi: int) -> OcrPage: ...


@runtime_checkable
class LayoutDetector(Protocol):
    """Region proposals for one cleaned page (page pixel space)."""

    name: str

    def detect(self, image: Path, *, ocr: OcrPage | None = None) -> list[RegionProposal]: ...


@dataclass(frozen=True, slots=True)
class FormulaJob:
    key: str                  # content-addressed (page + bbox + task)
    image: Path
    bbox: BBox
    task: str                 # "formula" | "ocr"


@runtime_checkable
class FormulaRecognizer(Protocol):
    """Math (and mixed math+prose lines) -> LaTeX. Returns ``{job.key: text}``; failed jobs are absent."""

    def recognize(self, jobs: Sequence[FormulaJob]) -> dict[str, str]: ...


# ---------------------------------------------------------------- outputs
@dataclass(frozen=True, slots=True)
class RenderRequest:
    item_id: str
    pages: Sequence[Path]             # cleaned page images, leaf order, access pages only
    hocr: Path                        # multi-page hOCR in the same order
    metadata: ItemMetadata
    formats: Sequence[str]            # subset of {"pdf", "pdfa", "epub"}
    extra: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class Renderer(Protocol):
    def render(self, req: RenderRequest) -> dict[str, Path]:
        """Returns ``{format: path}`` for every produced file."""
        ...


# ---------------------------------------------------------------- publishing
@dataclass(frozen=True, slots=True)
class PublishResult:
    entry_id: str
    url: str | None
    created: bool                     # False = an existing entry was updated (republish)
    acquisitions: dict[str, str]      # format -> catalog acquisition id


@runtime_checkable
class CatalogPublisher(Protocol):
    def publish(self, item_id: str, metadata: ItemMetadata, files: dict[str, Path],
                *, entry_id: str | None = None) -> PublishResult:
        """Create (``entry_id is None``) or update the catalog entry; idempotent."""
        ...


@runtime_checkable
class IaExporter(Protocol):
    """Builds ``out`` (a zip in IA item layout) from a book's data; returns the zip path.

    Plain data in, so the IA adapter needs no database access: the use case reads
    the rows and hands over metadata, leaves and files.
    """

    def export_item(self, item_id: str, metadata: ItemMetadata, leaves: Sequence[Leaf],
                    files: Mapping[str, Path], leaf_images: Mapping[int, Path], out: Path) -> Path:
        """``files``: artifact kind -> existing file; ``leaf_images``: leaf -> cleaned page."""
        ...


# ---------------------------------------------------------------- infrastructure
@runtime_checkable
class EventBus(Protocol):
    def publish(self, event: Event) -> None: ...
    def subscribe(self) -> Iterator[Event]: ...


@runtime_checkable
class TaskQueue(Protocol):
    """Schedules work; the application never imports Celery directly."""

    def enqueue_run(self, run_id: int, *, interactive: bool = False) -> str: ...
    def enqueue_discover(self, project: str, collection: str | None = None) -> str: ...
    def enqueue_export(self, project: str) -> str: ...
    def revoke(self, task_id: str) -> None: ...
