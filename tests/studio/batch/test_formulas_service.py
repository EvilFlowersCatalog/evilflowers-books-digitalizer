"""The formulas stage on a service, end to end (evilflowers-books-digitalizer#4, #6).

``evilflowers_equation.standalone.formulas`` (evilflowers-equation-service#8) reads crops of
the regions marked ``reocr`` and writes ``formulas.json``; the orchestrator judges every answer
(``fix_latex``, ``looks_hallucinated``), and a row the service marks ``disputed`` becomes a
disputed region with the service's reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from batch_harness import CLEAN, FORMULAS, HOCR, LAYOUT, PROJECT, Studio, make_studio

from evilflowers_books_digitalizer.db.models import Job, Region
from evilflowers_books_digitalizer.studio.domain import (
    BBox,
    BookStatus,
    JobStatus,
    PageStatus,
    RegionKind,
    RegionProposal,
    RegionSource,
    RegionStatus,
    RunKind,
    RunStatus,
)
from evilflowers_books_digitalizer.studio.testing import fakes
from evilflowers_books_digitalizer.studio.workers import context, tasks
from evilflowers_books_digitalizer.studio.workers.stages.formulas import region_id

FORMULA = RegionProposal(RegionKind.FORMULA, BBox(10, 160, 150, 200), RegionSource.HEURISTIC, 0.9)


def _studio(tmp_path: Path, *, answer: str = "\\[ E = mc^2 \\]",
            extra: list[RegionProposal] | None = None) -> Studio:
    studio = make_studio(tmp_path,
                         layout=fakes.FakeLayout(extra=[FORMULA] if extra is None else extra))
    studio.engines.formulas = fakes.FakeFormulaRecognizer(default=answer)
    tasks.discover.delay(PROJECT)
    return studio


def _jobs(studio: Studio, run_id: int, stage: str = "formulas") -> list[Job]:
    return studio.session().query(Job).filter_by(run_pk=run_id, stage=stage).order_by(Job.id).all()


def _formula_regions(studio: Studio) -> list[Region]:
    return studio.session().query(Region).filter_by(kind="formula").order_by(Region.id).all()


# ---------------------------------------------------------------- end to end
def test_formulas_on_the_service_fill_latex_and_consume_the_attempt(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error
    (job,) = _jobs(studio, run.id)                        # 4 leaves with a formula, batch 32
    assert (job.backend, job.task, job.status) == ("service", FORMULAS, JobStatus.APPLIED)
    assert len(job.params["regions"]) == 4
    assert {r.text for r in _formula_regions(studio)} == {"E = mc^2"}   # fix_latex applied
    assert not (tmp_path / job.params["output_dir"]).exists()
    assert studio.book().status == BookStatus.APPROVED


@pytest.mark.parametrize("answer, text, rejected", [
    ("\\[ E = mc^2 \\]", "E = mc^2", 0), ("Obr. 3 \\alpha", None, 4)])
def test_the_orchestrator_judges_every_answer(tmp_path: Path, answer: str, text: str | None,
                                              rejected: int) -> None:
    studio = _studio(tmp_path, answer=answer)
    studio.execute(RunKind.FULL)
    regions = _formula_regions(studio)
    assert {r.text for r in regions} == {text}
    assert all(("vlm rejected" in (r.reason or "")) == bool(rejected) for r in regions)
    book = studio.book()
    assert book.quality["signals"].get("vlm_rejected", 0) == rejected
    assert book.status == (BookStatus.REVIEW if rejected else BookStatus.APPROVED)


def test_a_disputed_answer_keeps_its_text_and_disputes_the_region(tmp_path: Path) -> None:
    """equation-service#8: its render check or cross-check doubts the answer."""
    studio = _studio(tmp_path)
    studio.engines.formulas.dispute = "SSIM 0.41 below 0.6"
    studio.execute(RunKind.FULL)
    regions = _formula_regions(studio)
    assert {(r.text, r.status) for r in regions} == {("E = mc^2", RegionStatus.DISPUTED)}
    assert all(r.reason and "vlm disputed: SSIM 0.41" in r.reason for r in regions)
    assert {p.status for p in studio.book().pages} == {PageStatus.DISPUTED}
    assert studio.book().status == BookStatus.REVIEW


def test_a_book_without_formulas_skips_the_stage(tmp_path: Path) -> None:
    studio = _studio(tmp_path, extra=[])
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
    studio = _studio(tmp_path)
    studio.engines.formulas = _OperatorMeanwhile(default="\\[ y \\]")
    assert studio.execute(RunKind.FULL).status == RunStatus.SUCCEEDED
    texts = [r.text for r in _formula_regions(studio)]
    assert texts[0] == "x_{operator}" and set(texts[1:]) == {"y"}


# ---------------------------------------------------------------- every service stage at once
def test_a_full_run_has_no_pixel_or_model_job_of_its_own(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error
    service = {(j.stage, j.task) for j in studio.session().query(Job).filter_by(
        run_pk=run.id, backend="service")}
    assert service == {("clean", CLEAN), ("analyze", HOCR), ("analyze", LAYOUT),
                       ("formulas", FORMULAS), ("render", "evilflowers_render.book")}
    local = {j.task for j in studio.session().query(Job).filter_by(run_pk=run.id,
                                                                    backend="local")}
    assert local == {"digitalizer.local.clean_marks", "digitalizer.local.title_page",
                     "digitalizer.local.render_prepare", "digitalizer.local.gate"}
    assert {r.source for r in _formula_regions(studio)} == {RegionSource.DOCLING}
