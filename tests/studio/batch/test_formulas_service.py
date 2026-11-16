"""The formulas stage on a service, end to end (evilflowers-books-digitalizer#4).

``evilflowers_equation.standalone.formulas`` (evilflowers-equation-service#8) reads crops of
the regions marked ``reocr`` and writes ``formulas.json``; the orchestrator applies the
answers through the in-process guards (``fix_latex``, ``looks_hallucinated``), so the quality
gate judges a service's answer exactly as an in-process one. Also: one full run with every
stage that has a service contract (clean, analyze, formulas) on stand-in services.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from batch_harness import PROJECT, Studio, make_studio

from evilflowers_books_digitalizer.db.models import Job, Region
from evilflowers_books_digitalizer.studio.config import (
    OrchestrationConfig,
    RoutePart,
    StageRoute,
)
from evilflowers_books_digitalizer.studio.domain import (
    BBox,
    BookStatus,
    JobStatus,
    RegionKind,
    RegionProposal,
    RegionSource,
    RegionStatus,
    RunKind,
    RunStatus,
)
from evilflowers_books_digitalizer.studio.testing import fakes, standalone
from evilflowers_books_digitalizer.studio.workers import context, tasks
from evilflowers_books_digitalizer.studio.workers.app import celery
from evilflowers_books_digitalizer.studio.workers.stages.formulas import region_id

FORMULAS = "evilflowers_equation.standalone.formulas"
CLEAN, HOCR, LAYOUT, ERASE = ("evilflowers_scan.clean", "evilflowers_ocr_worker.standalone.hocr",
                              "evilflowers_analyzer.standalone.layout", "evilflowers_scan.erase")
FORMULA = RegionProposal(RegionKind.FORMULA, BBox(10, 160, 150, 200), RegionSource.HEURISTIC, 0.9)


@pytest.fixture(autouse=True)
def _services() -> Iterator[None]:
    def env():  # type: ignore[no-untyped-def]
        ctx = context.current()
        assert ctx.config.storage_root is not None
        return ctx.config.storage_root, ctx.adapters(PROJECT)

    @celery.task(name=FORMULAS)
    def formulas(**m):  # type: ignore[no-untyped-def]
        root, a = env()
        return standalone.equation_formulas(root, a.formulas, **m)

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

    yield
    for name in (FORMULAS, CLEAN, HOCR, LAYOUT, ERASE):
        celery.tasks.pop(name, None)


def _routes(*services: str) -> OrchestrationConfig:
    routes = dict(OrchestrationConfig().routes)
    if "formulas" in services:
        routes["formulas"] = StageRoute("service", FORMULAS, "evilflowers_equation.standalone",
                                        "evilflowers_equation.standalone.interactive", 3600,
                                        batch=32)
    if "clean" in services:
        routes["clean"] = StageRoute("service", CLEAN, "evilflowers_scan.standalone",
                                     "evilflowers_scan.standalone.interactive", 3600, batch=32)
    if "analyze" in services:
        routes["analyze"] = StageRoute("service", "", "", "", 1800, batch=32, parts={
            "hocr": RoutePart(HOCR, "o", "o.i"), "layout": RoutePart(LAYOUT, "l", "l.i"),
            "erase": RoutePart(ERASE, "s", "s.i")})
    return OrchestrationConfig(routes=routes)


def _studio(tmp_path: Path, *services: str, answer: str = "\\[ E = mc^2 \\]",
            extra: list[RegionProposal] | None = None) -> Studio:
    studio = make_studio(tmp_path, orchestration=_routes(*services), storage_root=tmp_path,
                         layout=fakes.FakeLayout(extra=[FORMULA] if extra is None else extra))
    studio.adapters.formulas = fakes.FakeFormulaRecognizer(default=answer)
    tasks.discover.delay(PROJECT)
    return studio


def _jobs(studio: Studio, run_id: int, stage: str = "formulas") -> list[Job]:
    return studio.session().query(Job).filter_by(run_pk=run_id, stage=stage).order_by(Job.id).all()


def _formula_regions(studio: Studio) -> list[Region]:
    return studio.session().query(Region).filter_by(kind="formula").order_by(Region.id).all()


# ---------------------------------------------------------------- end to end
def test_formulas_on_the_service_fill_latex_and_consume_the_attempt(tmp_path: Path) -> None:
    studio = _studio(tmp_path, "formulas")
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error
    (job,) = _jobs(studio, run.id)                        # 4 leaves with a formula, batch 32
    assert (job.backend, job.task, job.status) == ("service", FORMULAS, JobStatus.APPLIED)
    assert len(job.params["regions"]) == 4
    assert {r.text for r in _formula_regions(studio)} == {"E = mc^2"}   # fix_latex applied
    assert not (tmp_path / job.params["output_dir"]).exists()
    assert studio.book().status == BookStatus.APPROVED


@pytest.mark.parametrize("answer", ["\\[ E = mc^2 \\]", "Obr. 3 \\alpha"])
def test_service_answers_are_judged_as_in_process_ones(tmp_path: Path, answer: str) -> None:
    def outcome(studio: Studio):  # type: ignore[no-untyped-def]
        book = studio.book()
        return ([(r.text, r.reason, r.status) for r in _formula_regions(studio)],
                book.status, book.quality["signals"].get("vlm_rejected"))

    local = _studio(tmp_path / "local", answer=answer)
    local.execute(RunKind.FULL)
    remote = _studio(tmp_path / "service", "formulas", answer=answer)
    remote.execute(RunKind.FULL)
    assert outcome(local) == outcome(remote)


def test_a_book_without_formulas_skips_the_stage(tmp_path: Path) -> None:
    studio = _studio(tmp_path, "formulas", extra=[])
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert _jobs(studio, run.id) == []
    assert _jobs(studio, run.id, "render")                # the run went on


class _OperatorMeanwhile(fakes.FakeFormulaRecognizer):
    """While the service recognizes, an operator confirms the first region with own text."""

    def recognize(self, jobs):  # type: ignore[no-untyped-def]
        s = context.current().session_factory()
        region = s.get(Region, region_id(jobs[0].key))
        region.status, region.text = str(RegionStatus.CONFIRMED), "x_{operator}"
        s.commit()
        return super().recognize(jobs)


def test_an_operator_decision_made_while_the_service_runs_wins(tmp_path: Path) -> None:
    studio = _studio(tmp_path, "formulas")
    studio.adapters.formulas = _OperatorMeanwhile(default="\\[ y \\]")
    assert studio.execute(RunKind.FULL).status == RunStatus.SUCCEEDED
    texts = [r.text for r in _formula_regions(studio)]
    assert texts[0] == "x_{operator}" and set(texts[1:]) == {"y"}


# ---------------------------------------------------------------- every service stage at once
def test_a_full_run_with_clean_analyze_and_formulas_on_services(tmp_path: Path) -> None:
    studio = _studio(tmp_path, "clean", "analyze", "formulas")
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert studio.book().status == BookStatus.APPROVED
    service = {(j.stage, j.task) for j in studio.session().query(Job).filter_by(
        run_pk=run.id, backend="service")}
    assert service == {("clean", CLEAN), ("analyze", HOCR), ("analyze", LAYOUT),
                       ("formulas", FORMULAS)}
    assert {r.text for r in _formula_regions(studio)} == {"E = mc^2"}
    assert {r.source for r in _formula_regions(studio)} == {RegionSource.DOCLING}
    local = {j.stage for j in studio.session().query(Job).filter_by(run_pk=run.id,
                                                                     backend="local")}
    assert local == {"render", "gate"}                    # what stays in-process for now
