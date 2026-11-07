"""Batch-side harness: an eager Celery app over SQLite, the in-memory bus and the shared fakes."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

os.environ.setdefault("STUDIO_EAGER", "1")   # before the Celery app is imported

from sqlalchemy.orm import sessionmaker  # noqa: E402

from evilflowers_books_digitalizer.db.models import Book, Run  # noqa: E402
from evilflowers_books_digitalizer.studio.adapters.artifacts import IaArtifactStore  # noqa: E402
from evilflowers_books_digitalizer.studio.adapters.eventbus import InMemoryEventBus  # noqa: E402
from evilflowers_books_digitalizer.studio.adapters.imaging import PillowImageOps  # noqa: E402
from evilflowers_books_digitalizer.studio.config import (  # noqa: E402
    ProjectConfig,
    PublishConfig,
    StudioConfig,
)
from evilflowers_books_digitalizer.studio.domain import RunKind, RunStatus  # noqa: E402
from evilflowers_books_digitalizer.studio.events import EventType  # noqa: E402
from evilflowers_books_digitalizer.studio.ports import OcrPage, ScannedBook  # noqa: E402
from evilflowers_books_digitalizer.studio.testing import fakes  # noqa: E402
from evilflowers_books_digitalizer.studio.workers import context  # noqa: E402
from evilflowers_books_digitalizer.studio.workers.app import celery  # noqa: E402
from evilflowers_books_digitalizer.studio.adapters.registry import Adapters  # noqa: E402

PROJECT = "stu"


@dataclass
class RecordingOcr(fakes.FakeOcr):
    """FakeOcr that remembers which image it read (to see erased OCR inputs)."""

    images: list[Path] = field(default_factory=list)

    def recognize(self, image: Path, *, language: str, dpi: int) -> OcrPage:
        self.images.append(image)
        return super().recognize(image, language=language, dpi=dpi)


@dataclass
class Studio:
    ctx: context.WorkerContext
    adapters: Adapters
    bus: InMemoryEventBus
    tmp: Path

    def session(self) -> Any:
        return self.ctx.session_factory()

    def book(self) -> Book:
        s = self.session()
        book = s.query(Book).filter_by(project=PROJECT).one()
        return book

    def new_run(self, kind: RunKind, *, pages: list[int] | None = None,
                interactive: bool = False) -> int:
        s = self.session()
        book = s.query(Book).filter_by(project=PROJECT).one()
        run = Run(book_pk=book.id, kind=str(kind), status=RunStatus.QUEUED, pages=pages,
                  interactive=interactive, created_by="tester")
        s.add(run)
        s.commit()
        return run.id

    def execute(self, kind: RunKind, **kwargs: Any) -> Run:
        from evilflowers_books_digitalizer.studio.workers import tasks

        run_id = self.new_run(kind, **kwargs)
        tasks.run.delay(run_id)
        return self.session().get(Run, run_id)

    def events(self, type_: EventType) -> list[Any]:
        return [e for e in self.bus.published if e.type == type_]


def make_studio(tmp_path: Path, *, auto_publish: str = "never",
                layout: fakes.FakeLayout | None = None) -> Studio:
    frames = fakes.make_frames(tmp_path / "scans" / "fei" / "BOOK1", 2)
    project = ProjectConfig(slug=PROJECT, name="Test", scans_root=tmp_path / "scans",
                            item_id="stu-{collection}-{book}", auto_publish=auto_publish,  # type: ignore[arg-type]
                            outputs=("pdf", "pdfa", "epub"),
                            publish=PublishConfig(target="fake"),
                            pipeline={"chunk_size": 3, "scantailor": {"output_dpi": 300}})
    config = StudioConfig(database_url="sqlite://", redis_url="memory://",
                          artifacts_root=tmp_path / "artifacts", cache_dir=tmp_path / "cache",
                          projects={PROJECT: project})
    adapters = Adapters(
        scans=fakes.FakeScanStore({"fei": [ScannedBook("fei", "BOOK1", frames[0].parent,
                                                       tuple(frames))]}),
        artifacts=IaArtifactStore(config.artifacts_root, PROJECT),
        cleaner=fakes.FakePageCleaner(dpi=300),
        image_ops=PillowImageOps(),
        ocr=RecordingOcr(),
        detectors=[layout or fakes.FakeLayout()],
        formulas=fakes.FakeFormulaRecognizer(),
        renderer=fakes.FakeRenderer(tmp_path / "rendered"),
        metadata=fakes.FakeMetadataProvider(),
        publisher=fakes.FakePublisher(),
    )
    engine = fakes.sqlite_session().get_bind()
    bus = InMemoryEventBus()
    ctx = context.WorkerContext(config, sessionmaker(engine, expire_on_commit=False), bus,
                                adapters_factory=lambda cfg, p: adapters)
    context.configure(ctx)
    return Studio(ctx, adapters, bus, tmp_path)


def eager_celery() -> None:
    celery.conf.task_always_eager = True
    celery.conf.task_eager_propagates = True
