"""Fixtures for the interactive side: Services over SQLite + fakes, a seeded book, a TestClient."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import sessionmaker

from evilflowers_books_digitalizer.db.models import Book, Page, Region
from evilflowers_books_digitalizer.studio.adapters.eventbus import InMemoryEventBus
from evilflowers_books_digitalizer.studio.adapters.queue import RecordingTaskQueue
from evilflowers_books_digitalizer.studio.api.app import create_app
from evilflowers_books_digitalizer.studio.adapters.ia import IaItemExporter
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.config import ProjectConfig, StudioConfig
from evilflowers_books_digitalizer.studio.testing import fakes


@dataclass
class Seeded:
    book_id: int
    page_ids: list[int]
    region_ids: list[int]     # [disputed stamp on page 0, proposed text on page 1]


@pytest.fixture
def bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture
def queue() -> RecordingTaskQueue:
    return RecordingTaskQueue()


@pytest.fixture
def config(tmp_path: Path) -> StudioConfig:
    def project(slug: str, auto_publish: str) -> ProjectConfig:
        return ProjectConfig(slug=slug, name=slug.upper(), scans_root=tmp_path / "scans",
                             collections={"fei": "FEI STU"}, auto_publish=auto_publish)  # type: ignore[arg-type]

    return StudioConfig(database_url="sqlite://", redis_url="redis://unused",
                        artifacts_root=tmp_path / "artifacts", cache_dir=tmp_path / "cache",
                        projects={"stu": project("stu", "review"), "quiet": project("quiet", "never")})


@pytest.fixture
def services(config: StudioConfig, queue: RecordingTaskQueue, bus: InMemoryEventBus,
             tmp_path: Path) -> Services:
    engine = fakes.sqlite_session().get_bind()
    return Services(config=config, sessions=sessionmaker(bind=engine, expire_on_commit=False),
                    queue=queue, bus=bus,
                    artifacts_for=lambda p: fakes.FakeArtifactStore(tmp_path / "artifacts" / p))


@pytest.fixture
def client(services: Services, tmp_path: Path) -> TestClient:
    app = create_app(services=services, ia_exporter=IaItemExporter(),
                     static_dir=tmp_path / "dist", ping_seconds=0.2)
    return TestClient(app, headers={"X-Operator": "jana"})


def _write_pdf(path: Path, pages: int) -> Path:
    images = [Image.new("RGB", (200, 300), (255, 255, 255)) for _ in range(pages)]
    images[0].save(path, "PDF", save_all=True, append_images=images[1:])
    return path


def seed_book(services: Services, tmp_path: Path, *, project: str = "stu", status: str = "review",
              external_id: str = "BOOK1", title: str = "Vektory", pages: bool = True,
              quality: dict[str, object] | None = None) -> Seeded:
    """A book with two leaves cut from one synthetic spread, cleaned images, a PDF, regions."""
    base = tmp_path / external_id
    frame = fakes.make_frames(base / "scans", 1)[0]
    item_id = f"{project}-fei-{external_id}".lower()
    pdf = _write_pdf(base / f"{item_id}.pdf", 2) if pages else None
    book = Book(source="fei", book_id=external_id, slug=f"{project}_fei_{external_id}",
                project=project, item_id=item_id, title=title, status=status,
                artifacts={"pdf": str(pdf)} if pdf else {}, stats={},
                item_metadata={"identifier": item_id, "title": title, "language": ["slo"]},
                quality=quality or {}, quality_score=(quality or {}).get("score"),
                source_dir=str(frame.parent))
    if pages:
        for leaf, (x0, side) in enumerate(((0, "LEFT"), (200, "RIGHT"))):
            clean = base / "clean" / f"{leaf:04d}.tif"
            clean.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(frame) as im:
                im.crop((x0, 0, x0 + 200, 300)).convert("L").save(clean)
            page = Page(index=leaf, source_path=str(frame), crop_box=[x0, 0, 200, 300],
                        hand_side=side, clean_path=str(clean), width=200, height=300, dpi=600,
                        status="disputed" if leaf == 0 else "analyzed", version=1,
                        ocr_text=f"text {leaf}")
            kind, status_ = ("stamp", "disputed") if leaf == 0 else ("text", "proposed")
            page.regions.append(Region(kind=kind, x0=10, y0=10, x1=60, y1=60, source="heuristic",
                                       confidence=0.9, status=status_,
                                       action="remove" if kind == "stamp" else "keep",
                                       reason="stamp would be removed" if leaf == 0 else None))
            book.pages.append(page)
    session = services.sessions()
    session.add(book)
    session.commit()
    seeded = Seeded(book.id, [p.id for p in book.pages], [p.regions[0].id for p in book.pages])
    session.close()
    return seeded


@pytest.fixture
def seeded(services: Services, tmp_path: Path) -> Seeded:
    return seed_book(services, tmp_path)
