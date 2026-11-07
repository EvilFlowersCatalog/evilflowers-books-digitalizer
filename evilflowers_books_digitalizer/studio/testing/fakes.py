"""Shared test doubles for every studio port — use these, do not write your own.

Each fake is deterministic, writes only under the directory it is given, and
records calls so tests can assert on them. ``sqlite_session()`` gives a fresh
in-memory database with the full schema (no Alembic needed in unit tests).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from evilflowers_books_digitalizer.db.models import Base
from evilflowers_books_digitalizer.studio.domain import (
    BBox,
    PageOverride,
    RegionKind,
    RegionProposal,
    RegionSource,
)
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.ports import (
    ARTIFACT_KINDS,
    CleanedPage,
    FormulaJob,
    LeafPlan,
    OcrPage,
    PublishResult,
    RenderRequest,
    ScannedBook,
)


def sqlite_session() -> Session:
    """In-memory SQLite with the full schema (StaticPool: shared across threads)."""
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)()


def make_frames(directory: Path, n: int, *, spread: bool = True, size: tuple[int, int] = (400, 300)
                ) -> list[Path]:
    """Synthetic TIFF frames: two light 'pages' on a dark bed when ``spread``."""
    directory.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(n):
        im = Image.new("RGB", size, (28, 28, 28))
        d = ImageDraw.Draw(im)
        w, h = size
        boxes = [(10, 10, w // 2 - 6, h - 10), (w // 2 + 6, 10, w - 10, h - 10)] if spread else \
            [(10, 10, w - 10, h - 10)]
        for x0, y0, x1, y1 in boxes:
            d.rectangle((x0, y0, x1, y1), fill=(245, 245, 240))
            d.text((x0 + 10, y0 + 10), f"frame {i}", fill=(0, 0, 0))
        path = directory / f"scan_{i + 1:04d}.tif"
        im.save(path)
        frames.append(path)
    return frames


@dataclass
class FakeScanStore:
    books_by_collection: dict[str, list[ScannedBook]] = field(default_factory=dict)

    def collections(self) -> list[str]:
        return sorted(self.books_by_collection)

    def books(self, collection: str) -> Iterator[ScannedBook]:
        yield from self.books_by_collection.get(collection, [])

    def book(self, collection: str, external_id: str) -> ScannedBook:
        for b in self.books_by_collection.get(collection, []):
            if b.external_id == external_id:
                return b
        raise KeyError(external_id)


@dataclass
class FakeArtifactStore:
    root: Path

    def item_dir(self, item_id: str) -> Path:
        d = self.root / item_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path(self, item_id: str, kind: str, *, leaf: int | None = None) -> Path:
        assert kind in ARTIFACT_KINDS, kind
        suffix = f"_{leaf:04d}" if leaf is not None else ""
        return self.item_dir(item_id) / f"{item_id}_{kind}{suffix}"

    def exists(self, item_id: str, kind: str, *, leaf: int | None = None) -> bool:
        return self.path(item_id, kind, leaf=leaf).exists()


@dataclass
class FakePageCleaner:
    """Splits every frame into two leaves (left/right halves); 'cleans' by copying the half."""

    dpi: int = 600
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def plan(self, frames: Sequence[Path]) -> list[LeafPlan]:
        self.calls.append(("plan", len(frames)))
        plans: list[LeafPlan] = []
        for frame in frames:
            with Image.open(frame) as im:
                w, h = im.size
            for side, (x0, x1) in enumerate(((0, w // 2), (w // 2, w))):
                plans.append(LeafPlan(len(plans), frame, side, BBox(x0, 0, x1, h), 0,
                                      "LEFT" if side == 0 else "RIGHT"))
        return plans

    def clean(self, plans: Sequence[LeafPlan], out_dir: Path,
              overrides: dict[int, PageOverride] | None = None) -> list[CleanedPage]:
        self.calls.append(("clean", [p.leaf_num for p in plans], dict(overrides or {})))
        out_dir.mkdir(parents=True, exist_ok=True)
        pages = []
        for p in plans:
            with Image.open(p.frame) as im:
                crop = im.crop((p.crop.x0, p.crop.y0, p.crop.x1, p.crop.y1)).convert("L")
            dst = out_dir / f"{p.leaf_num:04d}.tif"
            crop.save(dst)
            pages.append(CleanedPage(p.leaf_num, dst, crop.width, crop.height, self.dpi))
        return pages


@dataclass
class FakeOcr:
    text: str = "Toto je text strany."
    conf: float = 92.0

    def recognize(self, image: Path, *, language: str, dpi: int) -> OcrPage:
        with Image.open(image) as im:
            w, h = im.size
        hocr = (f"<div class='ocr_page' title='bbox 0 0 {w} {h}'><p class='ocr_par'>"
                f"<span class='ocr_line' title='bbox 10 10 {w - 10} 40; x_size 20'>"
                + "".join(f"<span class='ocrx_word' title='bbox 10 10 50 40; x_wconf {int(self.conf)}'>{t}</span> "
                          for t in self.text.split())
                + "</span></p></div>")
        return OcrPage(hocr, self.text, self.conf)


@dataclass
class FakeLayout:
    """Proposes a text block plus whatever ``extra`` regions a test injects."""

    name: str = "fake"
    extra: list[RegionProposal] = field(default_factory=list)

    def detect(self, image: Path, *, ocr: OcrPage | None = None) -> list[RegionProposal]:
        with Image.open(image) as im:
            w, h = im.size
        return [RegionProposal(RegionKind.TEXT, BBox(5, 5, w - 5, h // 2), RegionSource.HEURISTIC, 0.9),
                *self.extra]


@dataclass
class FakeFormulaRecognizer:
    answers: dict[str, str] = field(default_factory=dict)   # key -> latex; missing keys fail
    default: str | None = "x^2"

    def recognize(self, jobs: Sequence[FormulaJob]) -> dict[str, str]:
        out = {}
        for j in jobs:
            if j.key in self.answers:
                out[j.key] = self.answers[j.key]
            elif self.default is not None:
                out[j.key] = self.default
        return out


@dataclass
class FakeRenderer:
    out_dir: Path
    requests: list[RenderRequest] = field(default_factory=list)

    def render(self, req: RenderRequest) -> dict[str, Path]:
        self.requests.append(req)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        out = {}
        for fmt in req.formats:
            p = self.out_dir / f"{req.item_id}.{fmt}"
            p.write_bytes(f"{fmt}:{len(req.pages)} pages".encode())
            out[fmt] = p
        return out


@dataclass
class FakePublisher:
    published: list[tuple[str, str | None]] = field(default_factory=list)

    def publish(self, item_id: str, metadata: ItemMetadata, files: dict[str, Path],
                *, entry_id: str | None = None) -> PublishResult:
        self.published.append((item_id, entry_id))
        return PublishResult(entry_id or f"entry-{item_id}", f"https://catalog.test/{item_id}",
                             created=entry_id is None,
                             acquisitions={fmt: f"acq-{fmt}" for fmt in files})


@dataclass
class FakeMetadataProvider:
    title: str = "Testovacia kniha"

    def lookup(self, book: ScannedBook, *, ocr_text: str | None = None) -> ItemMetadata:
        return ItemMetadata(identifier=f"test-{book.collection}-{book.external_id}".lower(),
                            title=self.title, language=["slo"], date="2007")
