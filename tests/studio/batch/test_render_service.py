"""The render stage on render-service, end to end (evilflowers-books-digitalizer#4, #6).

Render is a chained stage: ``prepare`` (in-process: metadata lookup, the item's text documents,
the request) then ``book`` (evilflowers-render-service: the media). The media are renamed into
the IA item layout when ``book`` is applied, and the rows and IA documents are written by
``stages.render.finish``.
"""

from __future__ import annotations

from pathlib import Path

from batch_harness import BOOK, PROJECT, Studio, make_studio

from evilflowers_books_digitalizer.db.models import Job
from evilflowers_books_digitalizer.studio.config import RoutePart, _orchestration
from evilflowers_books_digitalizer.studio.domain import BookStatus, JobStatus, RunKind, RunStatus
from evilflowers_books_digitalizer.studio.orchestration import contract
from evilflowers_books_digitalizer.studio.testing import fakes
from evilflowers_books_digitalizer.studio.workers import tasks

MEDIA = {"pdf", "pdfa", "epub", "images"}


def _studio(tmp_path: Path) -> Studio:
    studio = make_studio(tmp_path)
    tasks.discover.delay(PROJECT)
    return studio


def _jobs(studio: Studio, run_id: int, stage: str = "render") -> list[Job]:
    return studio.session().query(Job).filter_by(run_pk=run_id, stage=stage).order_by(Job.id).all()


def _item_files(studio: Studio) -> dict[str, str]:
    """Artifact kind -> path relative to the item dir."""
    book = studio.book()
    return {k: str(Path(v).relative_to(book.output_dir)) for k, v in book.artifacts.items()}


# ---------------------------------------------------------------- end to end
def test_render_is_prepared_here_and_rendered_on_the_service(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
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


def test_a_render_run_alone_uses_the_service_and_replaces_the_media(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
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
    studio = _studio(tmp_path)
    studio.engines.renderer = _NoEpub(tmp_path / "rendered")
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.FAILED
    assert "missing ['epub']" in (run.error or "")
    assert not any(k in (studio.book().artifacts or {}) for k in MEDIA)
    (book,) = [j for j in _jobs(studio, run.id) if contract.part(j) == "book"]
    assert not (tmp_path / book.params["output_dir"]).exists()


# ---------------------------------------------------------------- configuration
def test_render_is_prepare_here_then_the_book_there() -> None:
    route = _orchestration({}).routes["render"]
    assert route.part("prepare") == RoutePart("digitalizer.local.render_prepare",
                                              "digitalizer.portal", "digitalizer.portal",
                                              backend="local")
    assert route.part("book").task == BOOK
    tuned = _orchestration({"stages": {"render": {"task": "other.book", "queue": "q"}}})
    assert (tuned.routes["render"].part("book").task,
            tuned.routes["render"].part("book").queue) == ("other.book", "q")
