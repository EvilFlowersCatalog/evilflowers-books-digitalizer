"""Batch-side harness: an eager Celery app over SQLite, the in-memory bus, the shared fakes and
stand-ins for every service the orchestrator sends jobs to.

The stand-ins are the reference tasks of :mod:`studio.testing.standalone`, registered under the
services' real task names, driving the engine doubles of the current :class:`Studio`
(``studio.engines``): scan-service (clean, erase), ocr-service (hOCR), analyzer-service
(layout), equation-service (formulas), render-service (book).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

os.environ.setdefault("STUDIO_EAGER", "1")   # before the Celery app is imported

from sqlalchemy.orm import sessionmaker  # noqa: E402

from evilflowers_books_digitalizer.db.models import Book, Run  # noqa: E402
from evilflowers_books_digitalizer.studio.adapters.artifacts import IaArtifactStore  # noqa: E402
from evilflowers_books_digitalizer.studio.adapters.eventbus import InMemoryEventBus  # noqa: E402
from evilflowers_books_digitalizer.studio.adapters.registry import Adapters  # noqa: E402
from evilflowers_books_digitalizer.studio.config import (  # noqa: E402
    ProjectConfig,
    PublishConfig,
    StudioConfig,
)
from evilflowers_books_digitalizer.studio.domain import RunKind, RunStatus  # noqa: E402
from evilflowers_books_digitalizer.studio.events import EventType  # noqa: E402
from evilflowers_books_digitalizer.studio.ports import Decider, ScannedBook  # noqa: E402
from evilflowers_books_digitalizer.studio.testing import fakes, standalone  # noqa: E402
from evilflowers_books_digitalizer.studio.testing.engines import OcrPage  # noqa: E402
from evilflowers_books_digitalizer.studio.testing.pillow_ops import PillowImageOps  # noqa: E402
from evilflowers_books_digitalizer.studio.workers import context  # noqa: E402
from evilflowers_books_digitalizer.studio.workers.app import celery  # noqa: E402

PROJECT = "stu"
CLEAN, ERASE = "evilflowers_scan.clean", "evilflowers_scan.erase"
HOCR = "evilflowers_ocr_worker.standalone.hocr"
LAYOUT = "evilflowers_analyzer.standalone.layout"
FORMULAS = "evilflowers_equation.standalone.formulas"
BOOK = "evilflowers_render.book"


@dataclass
class RecordingOcr(fakes.FakeOcr):
    """FakeOcr that remembers which image it read (to see masked OCR inputs)."""

    images: list[Path] = field(default_factory=list)

    def recognize(self, image: Path, *, language: str, dpi: int) -> OcrPage:
        self.images.append(image)
        return super().recognize(image, language=language, dpi=dpi)


@dataclass
class Engines:
    """What runs inside each stand-in service (swap one to make a service misbehave)."""

    cleaner: Any
    image_ops: Any
    ocr: Any
    detectors: list[Any]
    formulas: Any
    renderer: Any
    #: leaf -> the artefact marks the stand-in scan-service reports for it
    marks: dict[int, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass
class Studio:
    ctx: context.WorkerContext
    adapters: Adapters
    engines: Engines
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


_CURRENT: dict[str, Studio] = {}


def current() -> Studio:
    return _CURRENT["studio"]


def make_studio(tmp_path: Path, *, auto_publish: str = "never",
                layout: fakes.FakeLayout | None = None, publish_target: str = "fake",
                deciders: dict[str, Decider | None] | None = None,
                **config: Any) -> Studio:
    """``config`` overrides StudioConfig fields (scheduling, orchestration, marks, …)."""
    frames = fakes.make_frames(tmp_path / "scans" / "fei" / "BOOK1", 2)
    project = ProjectConfig(slug=PROJECT, name="Test", scans_root=tmp_path / "scans",
                            item_id="stu-{collection}-{book}", auto_publish=auto_publish,  # type: ignore[arg-type]
                            outputs=("pdf", "pdfa", "epub"),
                            publish=PublishConfig(target=publish_target),
                            pipeline={"scantailor": {"output_dpi": 300}})
    config.setdefault("storage_root", tmp_path)
    cfg = StudioConfig(database_url="sqlite://", redis_url="memory://",
                       artifacts_root=tmp_path / "artifacts", cache_dir=tmp_path / "cache",
                       projects={PROJECT: project}, **config)
    planner = fakes.FakePageCleaner(dpi=300)
    adapters = Adapters(
        scans=fakes.FakeScanStore({"fei": [ScannedBook("fei", "BOOK1", frames[0].parent,
                                                       tuple(frames))]}),
        artifacts=IaArtifactStore(cfg.artifacts_root, PROJECT),
        planner=planner,
        metadata=fakes.FakeMetadataProvider(),
        publisher=fakes.FakePublisher() if publish_target != "none" else None,
    )
    engines = Engines(cleaner=planner, image_ops=PillowImageOps(), ocr=RecordingOcr(),
                      detectors=[layout or fakes.FakeLayout()],
                      formulas=fakes.FakeFormulaRecognizer(),
                      renderer=fakes.FakeRenderer(tmp_path / "rendered"))
    engine = fakes.sqlite_session().get_bind()
    bus = InMemoryEventBus()
    ctx = context.WorkerContext(cfg, sessionmaker(engine, expire_on_commit=False), bus,
                                adapters_factory=lambda c, p: adapters,
                                deciders=deciders or {"text": None, "vision": None})
    context.configure(ctx)
    studio = Studio(ctx, adapters, engines, bus, tmp_path)
    _CURRENT["studio"] = studio
    return studio


# ---------------------------------------------------------------- stand-in services
def _root() -> Path:
    root = context.current().config.storage_root
    assert root is not None
    return root


SERVICES: dict[str, Callable[..., Any]] = {
    CLEAN: lambda **m: standalone.scan_clean(
        _root(), current().engines.cleaner, current().engines.image_ops,
        marks_of=lambda n: current().engines.marks.get(n, []), **m),
    ERASE: lambda **m: standalone.scan_erase(_root(), current().engines.image_ops, **m),
    HOCR: lambda **m: standalone.ocr_hocr(_root(), current().engines.ocr,
                                          current().engines.image_ops, **m),
    LAYOUT: lambda **m: standalone.analyzer_layout(_root(), current().engines.detectors,
                                                   current().engines.image_ops, **m),
    FORMULAS: lambda **m: standalone.equation_formulas(_root(), current().engines.formulas, **m),
    BOOK: lambda **m: standalone.render_book(_root(), current().engines.renderer, **m),
}


def install_services() -> None:
    """Register the stand-ins on the eager app under the services' task names (idempotent)."""
    for name, body in SERVICES.items():
        if name not in celery.tasks:
            celery.task(name=name)(_named(body, name))


def _named(body: Callable[..., Any], name: str) -> Callable[..., Any]:
    def task(**message: Any) -> Any:
        return body(**message)

    task.__name__ = task.__qualname__ = name.replace(".", "_")
    return task


def eager_celery() -> None:
    """Inline tasks; errors are not propagated (errbacks run instead, as in production)."""
    celery.conf.task_always_eager = True
    celery.conf.task_eager_propagates = False
    install_services()
