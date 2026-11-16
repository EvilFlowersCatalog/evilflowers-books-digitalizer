"""The render stage on a service, end to end (evilflowers-books-digitalizer#4).

Render on a service is a chained stage: ``prepare`` (in-process: metadata lookup, the item's
text documents, the request) then ``book`` (evilflowers-render-service: the media). The
media are renamed into the IA item layout when ``book`` is applied, and the rows and IA
documents are written by the same code as in-process (``stages.render.finish``).
Also: one full run with every stage that has a service on stand-in services.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from batch_harness import PROJECT, Studio, make_studio

from evilflowers_books_digitalizer.db.models import Job
from evilflowers_books_digitalizer.studio.config import (
    OrchestrationConfig,
    RoutePart,
    StageRoute,
    _orchestration,
)
from evilflowers_books_digitalizer.studio.domain import BookStatus, JobStatus, RunKind, RunStatus
from evilflowers_books_digitalizer.studio.orchestration import contract
from evilflowers_books_digitalizer.studio.testing import fakes, standalone
from evilflowers_books_digitalizer.studio.workers import context, tasks
from evilflowers_books_digitalizer.studio.workers.app import celery
from evilflowers_books_digitalizer.studio.workers.stages import render

BOOK = "evilflowers_render.book"
CLEAN, HOCR, LAYOUT, ERASE, FORMULAS = (
    "evilflowers_scan.clean", "evilflowers_ocr_worker.standalone.hocr",
    "evilflowers_analyzer.standalone.layout", "evilflowers_scan.erase",
    "evilflowers_equation.standalone.formulas")
MEDIA = {"pdf", "pdfa", "epub", "images"}


@pytest.fixture(autouse=True)
def _services() -> Iterator[None]:
    def env():  # type: ignore[no-untyped-def]
        ctx = context.current()
        assert ctx.config.storage_root is not None
        return ctx.config.storage_root, ctx.adapters(PROJECT)

    @celery.task(name=BOOK)
    def book(**m):  # type: ignore[no-untyped-def]
        root, a = env()
        return standalone.render_book(root, a.renderer, **m)

    @celery.task(name=CLEAN)
    def clean(**m):  # type: ignore[no-untyped-def]
        root, a = env()
        return standalone.scan_clean(root, a.cleaner, a.image_ops, **m)

    @celery.task(name=HOCR)
    def hocr(**m):  # type: ignore[no-untyped-def]
        root, a = env()
        return standalone.ocr_hocr(root, a.ocr, a.image_ops, **m)

    @celery.task(name=LAYOUT)
    def layout(**m):  # type: ignore[no-untyped-def]
        root, a = env()
        return standalone.analyzer_layout(root, a.detectors, a.image_ops, **m)

    @celery.task(name=ERASE)
    def erase(**m):  # type: ignore[no-untyped-def]
        root, a = env()
        return standalone.scan_erase(root, a.image_ops, **m)

    @celery.task(name=FORMULAS)
    def formulas(**m):  # type: ignore[no-untyped-def]
        root, a = env()
        return standalone.equation_formulas(root, a.formulas, **m)

    yield
    for name in (BOOK, CLEAN, HOCR, LAYOUT, ERASE, FORMULAS):
        celery.tasks.pop(name, None)


def _routes(*services: str) -> OrchestrationConfig:
    raw: dict[str, dict] = {}
    if "render" in services:
        raw["render"] = {"backend": "service", "task": BOOK, "queue": "evilflowers_render.standalone"}
    if "clean" in services:
        raw["clean"] = {"backend": "service", "task": CLEAN, "queue": "s"}
    if "analyze" in services:
        raw["analyze"] = {"backend": "service", "hocr": {"task": HOCR, "queue": "o"},
                          "layout": {"task": LAYOUT, "queue": "l"}, "erase": {"task": ERASE,
                                                                             "queue": "s"}}
    if "formulas" in services:
        raw["formulas"] = {"backend": "service", "task": FORMULAS, "queue": "e"}
    return _orchestration({"stages": raw})


def _studio(tmp_path: Path, *services: str) -> Studio:
    studio = make_studio(tmp_path, orchestration=_routes(*services), storage_root=tmp_path)
    tasks.discover.delay(PROJECT)
    return studio


def _jobs(studio: Studio, run_id: int, stage: str = "render") -> list[Job]:
    return studio.session().query(Job).filter_by(run_pk=run_id, stage=stage).order_by(Job.id).all()


def _item_files(studio: Studio) -> dict[str, str]:
    """Artifact kind -> path relative to the item dir (comparable across two studios)."""
    book = studio.book()
    return {k: str(Path(v).relative_to(book.output_dir)) for k, v in book.artifacts.items()}


# ---------------------------------------------------------------- end to end
def test_render_is_prepared_here_and_rendered_on_the_service(tmp_path: Path) -> None:
    studio = _studio(tmp_path, "render")
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert studio.book().status == BookStatus.APPROVED

    prepare, book = _jobs(studio, run.id)
    assert [(contract.part(j), j.backend, j.task, j.status) for j in (prepare, book)] == [
        ("prepare", "local", "digitalizer.local.render_prepare", JobStatus.APPLIED),
        ("book", "service", BOOK, JobStatus.APPLIED)]
    assert book.queue == "evilflowers_render.standalone"
    files = _item_files(studio)
    assert MEDIA <= set(files) and {"hocr", "text", "meta_xml", "files_xml"} <= set(files)
    item = studio.book().item_id
    assert files["pdf"] == f"{item}.pdf" and files["pdfa"] == f"{item}_archival.pdf"
    for path in studio.book().artifacts.values():
        assert Path(path).is_file(), path
    assert not (tmp_path / book.params["output_dir"]).exists()           # consumed
    requests = list((tmp_path / book.params["request"]).parent.glob("request-*.json"))
    assert [r.name for r in requests] == [f"request-{run.id}.json"]


def test_service_render_gives_the_in_process_item(tmp_path: Path) -> None:
    local = _studio(tmp_path / "local")
    local.execute(RunKind.FULL)
    remote = _studio(tmp_path / "service", "render")
    remote.execute(RunKind.FULL)
    a, b = local.book(), remote.book()
    assert _item_files(local) == _item_files(remote)
    for kind in ("pdf", "pdfa", "epub", "text", "hocr"):
        assert Path(a.artifacts[kind]).read_bytes() == Path(b.artifacts[kind]).read_bytes(), kind
    assert (a.item_metadata, a.title, a.n_pages) == (b.item_metadata, b.title, b.n_pages)
    assert a.stats[render.PDF_PAGES_STAT] == b.stats[render.PDF_PAGES_STAT]
    names = lambda book: Path(book.artifacts["files_xml"]).read_text().count("<file ")  # noqa: E731
    assert names(a) == names(b)


def test_a_render_run_alone_uses_the_service_and_replaces_the_media(tmp_path: Path) -> None:
    studio = _studio(tmp_path, "render")
    studio.execute(RunKind.FULL)
    before = Path(studio.book().artifacts["pdf"]).stat().st_mtime_ns
    again = studio.execute(RunKind.RENDER)
    assert again.status == RunStatus.SUCCEEDED, again.error
    assert [contract.part(j) for j in _jobs(studio, again.id)] == ["prepare", "book"]
    assert Path(studio.book().artifacts["pdf"]).stat().st_mtime_ns >= before
    requests = list(Path(studio.book().output_dir).rglob("request-*.json"))
    assert [r.name for r in requests] == [f"request-{again.id}.json"]   # the old one is gone


class _NoEpub(fakes.FakeRenderer):
    def render(self, req):  # type: ignore[no-untyped-def]
        out = super().render(req)
        out.pop("epub").unlink()
        return out


def test_a_render_missing_a_requested_format_fails_and_moves_nothing(tmp_path: Path) -> None:
    studio = _studio(tmp_path, "render")
    studio.adapters.renderer = _NoEpub(tmp_path / "rendered")
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.FAILED
    assert "missing ['epub']" in (run.error or "")
    assert not any(k in (studio.book().artifacts or {}) for k in MEDIA)
    (book,) = [j for j in _jobs(studio, run.id) if contract.part(j) == "book"]
    assert not (tmp_path / book.params["output_dir"]).exists()


# ---------------------------------------------------------------- configuration
def test_render_on_a_service_is_prepare_here_then_the_book_there() -> None:
    route = _routes("render").routes["render"]
    assert route.part("prepare") == RoutePart("digitalizer.local.render_prepare",
                                              "digitalizer.portal", "digitalizer.portal",
                                              backend="local")
    assert route.part("book").task == BOOK
    with pytest.raises(ValueError, match=r"render\.book\] with task and queue"):
        _orchestration({"stages": {"render": {"backend": "service"}}})
    assert isinstance(OrchestrationConfig().routes["render"], StageRoute)   # default: in-process


# ---------------------------------------------------------------- the whole book on services
def test_a_full_run_with_every_service_stage_on_services(tmp_path: Path) -> None:
    studio = _studio(tmp_path, "clean", "analyze", "formulas", "render")
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert studio.book().status == BookStatus.APPROVED
    jobs = studio.session().query(Job).filter_by(run_pk=run.id).all()
    local = sorted({(j.stage, contract.part(j)) for j in jobs if j.backend == "local"},
                   key=str)
    assert local == [("gate", None), ("render", "prepare")]   # the orchestrator's own steps
    assert MEDIA <= set(studio.book().artifacts)
