"""Domain vocabulary and business rules of the restoration studio — pure Python, no I/O.

Persistence uses the SQLAlchemy models in :mod:`evilflowers_books_digitalizer.db.models`
(the entities); every *rule* lives here as a pure function or value object so it is
testable without a database, a queue or a file system:

* the lifecycle of books, pages, regions and runs (allowed transitions);
* region fusion: proposals from several detectors -> one region set + disputes;
* the quality gate: auto-publish or send to an operator.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum


# ---------------------------------------------------------------- vocabulary
class BookStatus(StrEnum):
    DISCOVERED = "discovered"   # found in the TIFF tree, nothing scheduled
    QUEUED = "queued"           # a run is waiting for a worker
    RUNNING = "running"         # a run is executing
    REVIEW = "review"           # the quality gate flagged it: an operator decides
    APPROVED = "approved"       # reviewed (or gate passed), outputs final, not yet published
    PUBLISHED = "published"     # live in the catalog (Publication row has the entry)
    ERROR = "error"             # last run failed
    # legacy statuses written by the classic batch runner (db.Recorder)
    OK = "ok"


class PageStatus(StrEnum):
    PENDING = "pending"
    CLEANED = "cleaned"         # ScanTailor (or override geometry) applied, tiles exist
    ANALYZED = "analyzed"       # layout regions + OCR done
    DISPUTED = "disputed"       # at least one disputed region
    DONE = "done"


class RegionKind(StrEnum):
    """DocLayNet-compatible labels plus the restoration-specific ones."""

    TEXT = "text"
    TITLE = "title"
    SECTION_HEADER = "section_header"
    LIST_ITEM = "list_item"
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    FORMULA = "formula"
    TABLE = "table"
    PICTURE = "picture"
    PAGE_HEADER = "page_header"
    PAGE_FOOTER = "page_footer"
    # restoration: things that are on the scan but not part of the book
    STAMP = "stamp"
    STICKER = "sticker"
    HANDWRITING = "handwriting"
    OTHER = "other"


#: kinds that are never content; their default action is removal
NON_CONTENT = frozenset({RegionKind.STAMP, RegionKind.STICKER, RegionKind.HANDWRITING})

#: RegionKind -> DocLayNet class name (training export)
DOCLAYNET = {
    RegionKind.TEXT: "Text", RegionKind.TITLE: "Title", RegionKind.SECTION_HEADER: "Section-header",
    RegionKind.LIST_ITEM: "List-item", RegionKind.CAPTION: "Caption", RegionKind.FOOTNOTE: "Footnote",
    RegionKind.FORMULA: "Formula", RegionKind.TABLE: "Table", RegionKind.PICTURE: "Picture",
    RegionKind.PAGE_HEADER: "Page-header", RegionKind.PAGE_FOOTER: "Page-footer",
}


class RegionSource(StrEnum):
    HEURISTIC = "heuristic"     # hOCR + image heuristics (reflow.analyze)
    YOLO = "yolo"               # DocLayout-YOLO
    VLM = "vlm"                 # PaddleOCR-VL
    OPERATOR = "operator"       # drawn or relabelled by a person


class RegionStatus(StrEnum):
    PROPOSED = "proposed"       # a detector's guess, nobody objected
    DISPUTED = "disputed"       # detectors disagree / low confidence: needs a person
    CONFIRMED = "confirmed"     # an operator accepted it (or drew it)
    REJECTED = "rejected"       # an operator said "not a region"


class RegionAction(StrEnum):
    KEEP = "keep"               # normal content
    REMOVE = "remove"           # erase from the page (stamps, stickers, scribbles)
    AS_IMAGE = "as_image"       # keep as a picture crop (no OCR / no VLM)
    REOCR = "reocr"             # re-read with the VLM (formulas, tables)


class RunKind(StrEnum):
    FULL = "full"               # the whole book, from the TIFFs
    RERUN = "rerun"             # selected pages, overrides pinned (interactive priority)
    RENDER = "render"           # rebuild outputs only (after review edits)
    PUBLISH = "publish"         # push to the catalog


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReviewDecision(StrEnum):
    APPROVED = "approved"
    NEEDS_WORK = "needs_work"
    REJECTED = "rejected"       # not publishable (wrong book, damaged scan …)


# ---------------------------------------------------------------- lifecycles
_BOOK_TRANSITIONS: dict[BookStatus, frozenset[BookStatus]] = {
    BookStatus.DISCOVERED: frozenset({BookStatus.QUEUED}),
    BookStatus.QUEUED: frozenset({BookStatus.RUNNING, BookStatus.ERROR, BookStatus.DISCOVERED}),
    BookStatus.RUNNING: frozenset({BookStatus.REVIEW, BookStatus.APPROVED, BookStatus.ERROR,
                                   BookStatus.QUEUED}),
    BookStatus.REVIEW: frozenset({BookStatus.QUEUED, BookStatus.RUNNING, BookStatus.APPROVED}),
    BookStatus.APPROVED: frozenset({BookStatus.QUEUED, BookStatus.RUNNING, BookStatus.PUBLISHED,
                                    BookStatus.REVIEW}),
    BookStatus.PUBLISHED: frozenset({BookStatus.QUEUED, BookStatus.RUNNING, BookStatus.REVIEW,
                                     BookStatus.APPROVED}),
    BookStatus.ERROR: frozenset({BookStatus.QUEUED, BookStatus.DISCOVERED}),
    BookStatus.OK: frozenset({BookStatus.QUEUED, BookStatus.REVIEW, BookStatus.APPROVED,
                              BookStatus.PUBLISHED}),
}


class TransitionError(ValueError):
    pass


def check_transition(current: str, new: str) -> BookStatus:
    """Validate a book status change; returns the new status or raises TransitionError."""
    cur, nxt = BookStatus(current), BookStatus(new)
    if cur == nxt or nxt in _BOOK_TRANSITIONS[cur]:
        return nxt
    raise TransitionError(f"book cannot go from {cur} to {nxt}")


# ---------------------------------------------------------------- geometry
@dataclass(frozen=True, slots=True)
class BBox:
    """Axis-aligned box in *cleaned page* pixel coordinates (x0, y0) inclusive, (x1, y1) exclusive."""

    x0: int
    y0: int
    x1: int
    y1: int

    def __post_init__(self) -> None:
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError(f"empty bbox {self}")

    @property
    def area(self) -> int:
        return (self.x1 - self.x0) * (self.y1 - self.y0)

    def iou(self, other: BBox) -> float:
        ix = max(0, min(self.x1, other.x1) - max(self.x0, other.x0))
        iy = max(0, min(self.y1, other.y1) - max(self.y0, other.y0))
        inter = ix * iy
        return inter / (self.area + other.area - inter) if inter else 0.0

    def as_list(self) -> list[int]:
        return [self.x0, self.y0, self.x1, self.y1]

    @classmethod
    def of(cls, values: Sequence[float]) -> BBox:
        x0, y0, x1, y1 = (int(round(v)) for v in values)
        return cls(x0, y0, x1, y1)

    # IA scandata stores crops as x, y, w, h — the one *storage* form for crops
    # (pages.crop_box, Leaf.crop_box); BBox is the in-memory form.
    def to_xywh(self) -> list[int]:
        return [self.x0, self.y0, self.x1 - self.x0, self.y1 - self.y0]

    @classmethod
    def from_xywh(cls, values: Sequence[float]) -> BBox:
        x, y, w, h = (int(round(v)) for v in values)
        return cls(x, y, x + w, y + h)


# ---------------------------------------------------------------- region fusion
@dataclass(frozen=True, slots=True)
class RegionProposal:
    """What a detector says about one area of a page."""

    kind: RegionKind
    bbox: BBox
    source: RegionSource
    confidence: float = 1.0          # 0..1
    text: str | None = None          # OCR text / LaTeX when the detector produced it


@dataclass(frozen=True, slots=True)
class FusedRegion:
    kind: RegionKind
    bbox: BBox
    source: RegionSource
    confidence: float
    status: RegionStatus
    action: RegionAction
    text: str | None = None
    alternatives: tuple[RegionKind, ...] = ()   # what the other detector(s) thought
    reason: str = ""                            # why it is disputed (shown to the operator)


def default_action(kind: RegionKind) -> RegionAction:
    if kind in NON_CONTENT:
        return RegionAction.REMOVE
    if kind in (RegionKind.FORMULA, RegionKind.TABLE):
        return RegionAction.REOCR
    if kind == RegionKind.PICTURE:
        return RegionAction.AS_IMAGE
    return RegionAction.KEEP


@dataclass(frozen=True, slots=True)
class FusionPolicy:
    match_iou: float = 0.5        # boxes overlapping this much describe the same thing
    min_confidence: float = 0.55  # below this a lone proposal is disputed
    #: disagreements that do not matter for the output (both are reflowed as text)
    equivalent: frozenset[frozenset[RegionKind]] = frozenset({
        frozenset({RegionKind.TEXT, RegionKind.LIST_ITEM}),
        frozenset({RegionKind.TITLE, RegionKind.SECTION_HEADER}),
        frozenset({RegionKind.PAGE_HEADER, RegionKind.PAGE_FOOTER}),
    })

    def same_meaning(self, a: RegionKind, b: RegionKind) -> bool:
        return a == b or frozenset({a, b}) in self.equivalent


def fuse(proposals: Iterable[RegionProposal], policy: FusionPolicy = FusionPolicy()
         ) -> list[FusedRegion]:
    """Merge proposals from several detectors into one region set.

    Rules, in order:
    1. Operator proposals always win and are CONFIRMED.
    2. Proposals overlapping by >= ``match_iou`` are the same region; the most
       confident one is kept. If their kinds differ in a way that changes the
       output (e.g. FORMULA vs PICTURE, STAMP vs TEXT) the region is DISPUTED.
    3. A lone proposal below ``min_confidence`` is DISPUTED.
    4. A non-content kind (stamp, sticker, handwriting) is always DISPUTED when it
       was only proposed by a detector: removing pixels needs a person's consent
       the first time (a project preset can later auto-confirm).
    """
    items = sorted(proposals, key=lambda p: (p.source != RegionSource.OPERATOR, -p.confidence))
    clusters: list[list[RegionProposal]] = []
    for p in items:
        for cluster in clusters:
            if cluster[0].bbox.iou(p.bbox) >= policy.match_iou:
                cluster.append(p)
                break
        else:
            clusters.append([p])

    fused: list[FusedRegion] = []
    for cluster in clusters:
        head = cluster[0]
        others = tuple(dict.fromkeys(p.kind for p in cluster[1:] if p.kind != head.kind))
        if head.source == RegionSource.OPERATOR:
            fused.append(FusedRegion(head.kind, head.bbox, head.source, 1.0, RegionStatus.CONFIRMED,
                                     default_action(head.kind), head.text, others))
            continue
        status, reason = RegionStatus.PROPOSED, ""
        conflicting = [k for k in others if not policy.same_meaning(k, head.kind)]
        if conflicting:
            status, reason = RegionStatus.DISPUTED, f"detectors disagree: {head.kind} vs " + \
                ", ".join(conflicting)
        elif len(cluster) == 1 and head.confidence < policy.min_confidence:
            status, reason = RegionStatus.DISPUTED, f"low confidence {head.confidence:.2f}"
        elif head.kind in NON_CONTENT:
            status, reason = RegionStatus.DISPUTED, f"{head.kind} would be removed: confirm once"
        fused.append(FusedRegion(head.kind, head.bbox, head.source, head.confidence, status,
                                 default_action(head.kind), head.text, others, reason))
    return sorted(fused, key=lambda r: (r.bbox.y0, r.bbox.x0))


# ---------------------------------------------------------------- quality gate
@dataclass(frozen=True, slots=True)
class QualitySignals:
    """Numbers a run measures; the gate turns them into a decision."""

    pages: int
    ocr_mean_conf: float              # mean Tesseract word confidence over prose, 0..100
    disputed_regions: int
    formula_fallbacks: int            # formulas left as images (VLM failed / rejected)
    vlm_rejected: int                 # hallucination guard trips
    foreign_letters: int = 0          # non-Slovak letters left in prose (should be 0)
    unreadable_frames: int = 0
    missing_pages: int = 0            # frames that produced no page


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    min_ocr_conf: float = 80.0
    max_disputed: int = 0
    max_formula_fallback_ratio: float = 0.25   # of all formulas
    max_vlm_rejected: int = 2


@dataclass(frozen=True, slots=True)
class QualityReport:
    passed: bool
    flags: tuple[str, ...]
    signals: QualitySignals
    score: float                      # 0..1, for sorting the review queue (lower = worse)

    def as_dict(self) -> dict[str, object]:
        from dataclasses import asdict

        return {"passed": self.passed, "flags": list(self.flags), "score": round(self.score, 3),
                "signals": asdict(self.signals)}


def quality_gate(s: QualitySignals, formulas: int = 0, policy: QualityPolicy = QualityPolicy()
                 ) -> QualityReport:
    """Auto-publish only when every signal passes; otherwise list why (operator-readable)."""
    flags: list[str] = []
    if s.ocr_mean_conf < policy.min_ocr_conf:
        flags.append(f"low OCR confidence ({s.ocr_mean_conf:.0f} < {policy.min_ocr_conf:.0f})")
    if s.disputed_regions > policy.max_disputed:
        flags.append(f"{s.disputed_regions} disputed region(s)")
    if formulas and s.formula_fallbacks / formulas > policy.max_formula_fallback_ratio:
        flags.append(f"{s.formula_fallbacks}/{formulas} formulas left as images")
    if s.vlm_rejected > policy.max_vlm_rejected:
        flags.append(f"{s.vlm_rejected} VLM answers rejected as hallucinations")
    if s.foreign_letters:
        flags.append(f"{s.foreign_letters} non-Slovak letters in prose")
    if s.unreadable_frames or s.missing_pages:
        flags.append(f"{s.unreadable_frames} unreadable frame(s), {s.missing_pages} missing page(s)")
    # score: mean of normalized goodness terms (1 = perfect)
    terms = [
        min(1.0, s.ocr_mean_conf / 100),
        1 / (1 + s.disputed_regions),
        1 - (s.formula_fallbacks / formulas if formulas else 0),
        1 / (1 + s.vlm_rejected + s.foreign_letters + s.unreadable_frames + s.missing_pages),
    ]
    return QualityReport(not flags, tuple(flags), s, sum(terms) / len(terms))


# ---------------------------------------------------------------- overrides
@dataclass(frozen=True, slots=True)
class PageOverride:
    """Operator settings pinned to one page; re-runs apply them before the detectors.

    Geometry is in *original frame* pixels (the crop the page is cut from); ``None``
    means "use the automatic value".
    """

    crop: BBox | None = None          # in the frame
    deskew_deg: float | None = None
    split_x: int | None = None        # gutter for a spread
    despeckle: str | None = None      # off | cautious | normal | aggressive
    color_mode: str | None = None     # mixed | color_grayscale | black_and_white
    ocr_language: str | None = None   # e.g. "slk+eng"
    extra: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {k: v for k, v in {
            "crop": self.crop.as_list() if self.crop else None, "deskew_deg": self.deskew_deg,
            "split_x": self.split_x, "despeckle": self.despeckle, "color_mode": self.color_mode,
            "ocr_language": self.ocr_language}.items() if v is not None}
        if self.extra:
            out["extra"] = dict(self.extra)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, object] | None) -> PageOverride:
        d = dict(d or {})
        crop = d.get("crop")
        return cls(
            crop=BBox.of(crop) if isinstance(crop, list | tuple) else None,
            deskew_deg=float(d["deskew_deg"]) if d.get("deskew_deg") is not None else None,  # type: ignore[arg-type]
            split_x=int(d["split_x"]) if d.get("split_x") is not None else None,  # type: ignore[call-overload]
            despeckle=d.get("despeckle"),  # type: ignore[arg-type]
            color_mode=d.get("color_mode"),  # type: ignore[arg-type]
            ocr_language=d.get("ocr_language"),  # type: ignore[arg-type]
            extra=dict(d.get("extra") or {}),  # type: ignore[call-overload]
        )
