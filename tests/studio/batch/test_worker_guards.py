"""Run guards that eager end-to-end runs never reach: holds, races, redeliveries, geometry."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from batch_harness import PROJECT, Studio, make_studio

from evilflowers_books_digitalizer.db.models import Page, Region, Run
from evilflowers_books_digitalizer.studio.domain import (
    BBox,
    PageOverride,
    RegionKind,
    RegionProposal,
    RegionSource,
    RunKind,
    RunStatus,
)
from evilflowers_books_digitalizer.studio.ports import CleanedPage, FormulaJob, LeafPlan
from evilflowers_books_digitalizer.studio.testing import fakes
from evilflowers_books_digitalizer.studio.workers import tasks
from evilflowers_books_digitalizer.studio.workers.app import celery
from evilflowers_books_digitalizer.studio.workers.stages.clean import MOVED_REASON


# ---------------------------------------------------------------- publish hold
def test_held_publish_resends_itself_without_spending_retries(
        discovered: Studio, monkeypatch: pytest.MonkeyPatch) -> None:
    """More waits than MAX_RETRIES: each is a fresh message, the run stays queued."""
    render_id = discovered.new_run(RunKind.RENDER)
    publish_id = discovered.new_run(RunKind.PUBLISH)
    sent: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(celery.conf, "task_always_eager", False)
    monkeypatch.setattr(tasks.run, "apply_async", lambda args, **kw: sent.append((args, kw)))
    for _ in range(tasks.MAX_RETRIES + 3):
        out = tasks.run(publish_id)
        assert out == {"run_id": publish_id, "skipped": True, "waiting_for": render_id}
    assert len(sent) == tasks.MAX_RETRIES + 3
    assert all(args == (publish_id,) and kw["countdown"] == tasks.WAIT_SECONDS
               for args, kw in sent)
    assert discovered.session().get(Run, publish_id).status == RunStatus.QUEUED


def test_held_publish_in_eager_mode_fails_instead_of_recursing(discovered: Studio) -> None:
    discovered.new_run(RunKind.RENDER)
    publish_id = discovered.new_run(RunKind.PUBLISH)
    assert tasks.run.delay(publish_id).get()["skipped"] is True
    run = discovered.session().get(Run, publish_id)
    assert run.status == RunStatus.FAILED and "waits for run" in (run.error or "")


# ---------------------------------------------------------------- dispatch / follow-ups
def test_failed_canvas_dispatch_fails_the_run(discovered: Studio,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def apply_async(self) -> None:
            raise ConnectionError("broker gone")

    monkeypatch.setattr(tasks, "canvas", lambda plan: Broken())
    with pytest.raises(ConnectionError):
        discovered.execute(RunKind.FULL)
    run = discovered.session().query(Run).one()
    assert run.status == RunStatus.FAILED and "broker gone" in (run.error or "")


def test_failed_follow_up_enqueue_fails_the_publish_row(
        discovered: Studio, monkeypatch: pytest.MonkeyPatch) -> None:
    """A queued row with no task behind it would 409 every later run of the book."""
    from evilflowers_books_digitalizer.studio.workers import context

    publish_id = discovered.new_run(RunKind.PUBLISH)

    def refuse(*_: Any, **__: Any) -> None:
        raise ConnectionError("broker gone")

    monkeypatch.setattr(tasks.run, "apply_async", refuse)
    tasks._dispatch_follow_ups(context.current(), [publish_id])
    run = discovered.session().get(Run, publish_id)
    assert run.status == RunStatus.FAILED and "broker gone" in (run.error or "")


def test_gate_retry_reuses_its_publish_run(tmp_path: Path) -> None:
    """Body committed, finish failed: the retried gate must not insert a second publish."""
    from evilflowers_books_digitalizer.studio.workers import context
    from evilflowers_books_digitalizer.studio.workers.stages.gate import gate

    studio = make_studio(tmp_path, auto_publish="gate")
    tasks.discover.delay(PROJECT)
    full = studio.execute(RunKind.FULL)
    s = studio.session()
    s.get(Run, full.id).status = RunStatus.RUNNING        # as if the finish never committed
    for r in s.query(Run).filter_by(kind="publish"):
        r.status = RunStatus.QUEUED
    s.commit()
    first = gate(context.current(), full.id)
    second = gate(context.current(), full.id)
    assert first == second and len(first) == 1
    assert studio.session().query(Run).filter_by(kind="publish").count() == 1


def test_publish_retry_after_recorded_publication_does_not_republish(discovered: Studio) -> None:
    from evilflowers_books_digitalizer.studio.workers import context
    from evilflowers_books_digitalizer.studio.workers.stages.publish import publish

    discovered.execute(RunKind.FULL)
    run = discovered.execute(RunKind.PUBLISH)
    s = discovered.session()
    s.get(Run, run.id).status = RunStatus.RUNNING         # finish lost after _record committed
    s.commit()
    publish(context.current(), run.id)
    publisher: fakes.FakePublisher = discovered.adapters.publisher  # type: ignore[assignment]
    assert len(publisher.published) == 1


# ---------------------------------------------------------------- formulas race
def test_formulas_keep_a_region_the_operator_decided_meanwhile(tmp_path: Path) -> None:
    formula = RegionProposal(RegionKind.FORMULA, BBox(10, 160, 150, 200), RegionSource.HEURISTIC,
                             0.9)
    studio = make_studio(tmp_path, layout=fakes.FakeLayout(extra=[formula]))

    class Slow(fakes.FakeFormulaRecognizer):
        def recognize(self, jobs: Sequence[FormulaJob]) -> dict[str, str]:
            s = studio.session()                          # the operator, in the SPA
            for job in jobs:
                region = s.get(Region, int(job.key.removeprefix("region-")))
                region.status, region.text, region.source = "confirmed", "a+b", "operator"
            s.commit()
            return super().recognize(jobs)

    studio.adapters.formulas = Slow(default=None)          # no answer: would wipe the text
    tasks.discover.delay(PROJECT)
    studio.execute(RunKind.FULL)
    formulas = studio.session().query(Region).filter_by(kind="formula").all()
    assert formulas and all(r.text == "a+b" and r.status == "confirmed" for r in formulas)


# ---------------------------------------------------------------- geometry change
class ShrinkingCleaner(fakes.FakePageCleaner):
    """Honours a crop override by producing a smaller page (as ScanTailor would)."""

    def clean(self, plans: Sequence[LeafPlan], out_dir: Path,
              overrides: dict[int, PageOverride] | None = None) -> list[CleanedPage]:
        pages = super().clean(plans, out_dir, overrides)
        out = []
        for page in pages:
            if overrides and page.leaf_num in overrides:
                from PIL import Image

                with Image.open(page.image) as im:
                    small = im.crop((0, 0, im.width // 2, im.height // 2))
                small.save(page.image)
                page = CleanedPage(page.leaf_num, page.image, small.width, small.height,
                                   page.dpi)
            out.append(page)
        return out


def test_rerun_that_changes_geometry_disputes_operator_regions(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    studio.adapters.cleaner = ShrinkingCleaner(dpi=300)
    tasks.discover.delay(PROJECT)
    studio.execute(RunKind.FULL)
    s = studio.session()
    page = s.query(Page).filter_by(index=1).one()
    width, height = page.width or 0, page.height or 0
    page.override = PageOverride(crop=BBox(200, 0, 300, 150)).as_dict()
    page.regions.append(Region(kind="stamp", x0=width - 40, y0=height - 40, x1=width,
                               y1=height, source="operator", status="confirmed",
                               action="remove", author="op"))
    s.commit()

    run = studio.execute(RunKind.RERUN, pages=[1], interactive=True)
    assert run.status == RunStatus.SUCCEEDED, run.error

    page = studio.session().query(Page).filter_by(index=1).one()
    stamp = next(r for r in page.regions if r.kind == "stamp")
    assert stamp.status == "disputed" and stamp.reason == MOVED_REASON   # kept, not replaced
    assert stamp.x1 <= (page.width or 0) and stamp.y1 <= (page.height or 0)  # clamped
    assert page.status == "disputed"
    untouched = studio.session().query(Page).filter_by(index=0).one()
    assert all(r.status != "disputed" or r.source != "operator" for r in untouched.regions)
