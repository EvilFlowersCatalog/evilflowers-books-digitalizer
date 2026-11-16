"""Scan marks across the whole book (evilflowers-books-digitalizer#14) and their labels (#7).

The stand-in scan-service reports marks per leaf with a dHash; after the clean stage the
orchestrator's ``clean/marks`` step groups them across the book, hides a group seen on enough
leaves without a model, and asks decider-vision once per uncertain group.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from batch_harness import PROJECT, Studio, make_studio

from evilflowers_books_digitalizer.db.models import Job, MarkGroup, Region
from evilflowers_books_digitalizer.studio import marks as m
from evilflowers_books_digitalizer.studio.adapters.queue import RecordingTaskQueue
from evilflowers_books_digitalizer.studio.application import labelling
from evilflowers_books_digitalizer.studio.application import marks as marks_uc
from evilflowers_books_digitalizer.studio.application.errors import BadRequest
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.config import MarksConfig
from evilflowers_books_digitalizer.studio.domain import (
    RegionAction,
    RegionKind,
    RegionSource,
    RegionStatus,
    RunKind,
    RunStatus,
)
from evilflowers_books_digitalizer.studio.orchestration import contract
from evilflowers_books_digitalizer.studio.testing import fakes
from evilflowers_books_digitalizer.studio.workers import tasks

STAMP = "f0f0f0f00f0f0f0f"          # one library stamp …
STAMP_NOISY = "f0f0f0f00f0f0f0e"    # … the same stamp scanned again (1 bit off)
STICKER = "0123456789abcdef"        # something else entirely


def _mark(dhash: str, *, band: str = "review", category: str = "stamp", p: float = 0.6,
          bbox: tuple[int, int, int, int] = (10, 10, 60, 60), aspect: float = 1.0) -> dict:
    return {"bbox": list(bbox), "category": category, "p": p, "band": band, "model": "signals",
            "signals": {"dhash": dhash, "aspect": aspect, "position": "margin"}}


def _studio(tmp_path: Path, *, vision: Any = None, **config: Any) -> Studio:
    studio = make_studio(tmp_path, deciders={"text": None, "vision": vision}, **config)
    tasks.discover.delay(PROJECT)
    return studio


def _groups(studio: Studio) -> list[MarkGroup]:
    return studio.session().query(MarkGroup).order_by(MarkGroup.id).all()


def _services(studio: Studio) -> Services:
    return Services(config=studio.ctx.config, sessions=studio.ctx.session_factory,
                    queue=RecordingTaskQueue(), bus=studio.bus,
                    artifacts_for=lambda p: studio.adapters.artifacts)


# ---------------------------------------------------------------- pure grouping
def _ref(leaf: int, dhash: str, **kw: Any) -> m.MarkRef:
    ref = m.ref_of(leaf, 0, _mark(dhash, **kw))
    assert ref is not None
    return ref


def test_grouping_by_hash_aspect_and_category() -> None:
    cfg = MarksConfig()
    marks = [_ref(0, STAMP), _ref(5, STAMP_NOISY), _ref(9, STAMP),
             _ref(3, STICKER), _ref(4, STAMP, category="sticker"),
             _ref(7, STAMP, aspect=3.0)]
    groups = m.assign(marks, [], cfg)
    assert sorted(sorted(r.leaf for r in g.members) for g in groups) == [[0, 5, 9], [3], [4], [7]]


def test_an_existing_group_keeps_its_members_and_gathers_new_ones() -> None:
    cfg = MarksConfig()
    old = m.GroupKey(7, "stamp", int(STAMP, 16), 1.0)
    kept = m.ref_of(2, 0, {**_mark(STICKER), "group": 7})   # an operator put it there
    new = _ref(4, STAMP_NOISY)
    groups = m.assign([kept, new], [old], cfg)               # type: ignore[list-item]
    assert [(g.id, sorted(r.leaf for r in g.members)) for g in groups] == [(7, [2, 4])]


def test_marks_without_a_hash_or_of_an_ungrouped_category_are_left_alone() -> None:
    assert m.ref_of(0, 0, _mark(STAMP, category="edge")) is None
    assert m.ref_of(0, 0, {**_mark(STAMP), "signals": {}}) is None
    assert m.band_of("stamp", 0.95, MarksConfig()) == "review"          # not calibrated yet
    assert m.band_of("stamp", 0.95, MarksConfig(hide_above=0.9)) == "hide"
    assert m.band_of("content", 0.95, MarksConfig(keep_above=0.9)) == "keep"


# ---------------------------------------------------------------- end to end
def test_a_stamp_on_enough_leaves_is_hidden_without_a_model(tmp_path: Path) -> None:
    vision = fakes.FakeDecider()
    studio = _studio(tmp_path, vision=vision)
    for leaf in range(4):                                 # one stamp per leaf: 4 >= min_leaves 3
        studio.engines.marks[leaf] = [_mark(STAMP if leaf % 2 else STAMP_NOISY)]
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error

    (group,) = _groups(studio)
    assert (group.leaves, group.members, group.band, group.decision, group.source) == (
        4, 4, "hide", m.REPEATED, "rule")
    assert vision.calls == []                             # no decider-vision call at all
    for page in studio.book().pages:
        (mark,) = page.marks or []
        assert (mark["group"], mark["band"], mark["decision"]) == (group.id, "hide", "repeated")
        stamp = [r for r in page.regions if r.source == RegionSource.SCAN]
        assert [(r.kind, r.status, r.action) for r in stamp] == [
            (RegionKind.STAMP, RegionStatus.DISPUTED, RegionAction.REMOVE)]   # the operator decides
    erase = [j for j in studio.session().query(Job).filter_by(run_pk=run.id)
             if contract.part(j) == "erase"]
    assert erase == []                                    # hide never erases by itself
    assert "1 group(s), 1 repeated" in str(studio.session().get(type(run), run.id).log)


def test_an_uncertain_group_is_asked_once_and_the_verdict_is_every_members(
        tmp_path: Path) -> None:
    vision = fakes.FakeDecider(answers={m.SCAN_MARK: ("sticker", 0.93)})
    studio = _studio(tmp_path, vision=vision, marks=MarksConfig(hide_above=0.9))
    studio.engines.marks[0] = [_mark(STAMP, p=0.5, bbox=(10, 10, 40, 40))]
    studio.engines.marks[2] = [_mark(STAMP_NOISY, p=0.7, bbox=(12, 12, 42, 42))]
    studio.engines.marks[3] = [_mark(STICKER, band="keep", p=0.9)]   # sure content: not asked
    assert studio.execute(RunKind.FULL).status == RunStatus.SUCCEEDED

    asked, other = _groups(studio)
    assert len(vision.calls) == 1                         # once for two occurrences
    call = vision.calls[0]
    assert call["question"] == "scan_mark" and set(call["criteria"]) == set(m.SCAN_MARK_CRITERIA)
    assert call["images"][0][:4] == b"\x89PNG"
    assert (asked.crop_leaf, asked.crop_bbox) == (2, [12, 12, 42, 42])   # the clearest one
    assert (asked.source, asked.decision, asked.p, asked.band, asked.decider_model) == (
        "decider", "sticker", 0.93, "hide", "fake-decider@1")
    for leaf in (0, 2):
        (mark,) = next(p for p in studio.book().pages if p.index == leaf).marks or []
        assert (mark["category"], mark["band"], mark["decision"], mark["p"]) == (
            "sticker", "hide", "sticker", 0.93)
    assert (other.source, other.band) == ("signals", "keep")


def test_without_a_decider_or_when_it_fails_marks_stay_review_and_the_run_goes_on(
        tmp_path: Path) -> None:
    off = _studio(tmp_path / "off")
    off.engines.marks[0] = [_mark(STAMP)]
    assert off.execute(RunKind.FULL).status == RunStatus.SUCCEEDED
    (group,) = _groups(off)
    assert (group.source, group.band, group.decider_error) == ("signals", "review", None)

    down = fakes.FakeDecider(fail=True)
    studio = _studio(tmp_path / "down", vision=down)
    studio.engines.marks[0] = [_mark(STAMP)]
    assert studio.execute(RunKind.FULL).status == RunStatus.SUCCEEDED
    (group,) = _groups(studio)
    assert group.band == "review" and "timed out" in (group.decider_error or "")
    assert (studio.book().pages[0].marks or [])[0]["band"] == "review"
    down.fail = False                                     # the next run asks again
    assert studio.execute(RunKind.RERUN, pages=[1]).status == RunStatus.SUCCEEDED
    (group,) = _groups(studio)
    assert (len(down.calls), group.source, group.decider_error) == (2, "decider", None)


def test_repetition_never_hides_colourless_marks_or_content(tmp_path: Path) -> None:
    """A running head or page number repeats on every leaf too (scan-service: COLOURLESS_CAP);
    a mark judged content stays content however often it repeats."""
    vision = fakes.FakeDecider(answers={m.SCAN_MARK: ("content", 0.9)})
    studio = _studio(tmp_path, vision=vision)
    for leaf in range(4):
        colourless = _mark(STAMP)
        colourless["signals"]["colour"] = False
        studio.engines.marks[leaf] = [colourless,
                                      _mark(STICKER, band="keep", bbox=(100, 100, 150, 150))]
    assert studio.execute(RunKind.FULL).status == RunStatus.SUCCEEDED
    head, kept = _groups(studio)
    assert (head.leaves, head.source, head.decision, head.band) == (4, "decider", "content",
                                                                    "review")
    assert (kept.leaves, kept.source, kept.band) == (4, "signals", "keep")
    assert len(vision.calls) == 1                          # the colourless group, once


def test_a_dead_decider_costs_one_call_not_one_per_group(tmp_path: Path) -> None:
    down = fakes.FakeDecider(fail=True)
    studio = _studio(tmp_path, vision=down)
    studio.engines.marks[0] = [_mark("1111111111111111"),
                               _mark("2222222222222222", bbox=(100, 10, 150, 60)),
                               _mark("4444444444444444", bbox=(10, 100, 60, 150))]
    assert studio.execute(RunKind.FULL).status == RunStatus.SUCCEEDED
    assert len(down.calls) == 1
    groups = _groups(studio)
    assert sum(g.decider_error is not None for g in groups) == 1
    assert {g.source for g in groups if g.decider_error is None} == {"signals"}   # asked next run


def test_a_reclean_keeps_the_group_and_its_verdict(tmp_path: Path) -> None:
    vision = fakes.FakeDecider(answers={m.SCAN_MARK: ("stamp", 0.8)})
    studio = _studio(tmp_path, vision=vision)
    studio.engines.marks[0] = [_mark(STAMP)]
    studio.execute(RunKind.FULL)
    (before,) = _groups(studio)
    studio.engines.marks[1] = [_mark(STAMP_NOISY)]        # the re-run finds it on leaf 1 too
    studio.execute(RunKind.RERUN, pages=[1])
    (after,) = _groups(studio)
    assert after.id == before.id and (after.members, after.decision) == (2, "stamp")
    assert len(vision.calls) == 1                         # the new occurrence takes the verdict
    assert (studio.book().pages[1].marks or [])[0]["decision"] == "stamp"


# ---------------------------------------------------------------- the operator
def test_the_operator_splits_a_group_and_overrules_a_verdict(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    for leaf in range(4):
        studio.engines.marks[leaf] = [_mark(STAMP)]
    studio.execute(RunKind.FULL)
    (group,) = _groups(studio)
    svc = _services(studio)

    new = marks_uc.split(svc, group.id, marks_uc.MarkGroupSplit(leaves=[3]), "jana")
    assert (new.on_leaves, new.source, new.band, new.split_from) == ([3], "operator", "review",
                                                                     group.id)
    kept = marks_uc.list_groups(svc, studio.book().id)
    assert [(g.on_leaves, g.band) for g in kept] == [([0, 1, 2], "hide"), ([3], "review")]
    with pytest.raises(BadRequest):
        marks_uc.split(svc, new.id, marks_uc.MarkGroupSplit(leaves=[3]), "jana")

    decided = marks_uc.decide(svc, group.id, marks_uc.MarkGroupPatch(band="keep"), "jana")
    assert (decided.band, decided.decision, decided.source) == ("keep", "content", "operator")
    assert [(p.marks or [])[0]["band"] for p in studio.book().pages] == ["keep"] * 3 + ["review"]
    assert marks_uc.image(svc, group.id)[:4] == b"\x89PNG"

    studio.execute(RunKind.RERUN, pages=[0, 1, 2, 3])     # regrouping leaves operators alone
    assert [(g.on_leaves, g.band, g.source) for g in marks_uc.list_groups(svc, studio.book().id)
            ] == [([0, 1, 2], "keep", "operator"), ([3], "review", "operator")]
    regions = studio.session().query(Region).filter_by(source=str(RegionSource.SCAN)).all()
    assert len(regions) == 1                              # keep is not proposed; review is


def test_metrics_report_groups_and_calls_saved(tmp_path: Path) -> None:
    from evilflowers_books_digitalizer.studio import ledger_metrics

    studio = _studio(tmp_path, vision=fakes.FakeDecider())
    for leaf in range(4):
        studio.engines.marks[leaf] = [_mark(STAMP)]
    studio.engines.marks[0].append(_mark(STICKER, bbox=(100, 100, 150, 150)))
    studio.execute(RunKind.FULL)
    text = ledger_metrics.render(ledger_metrics.LedgerCollector(
        studio.ctx.session_factory, lambda: studio.ctx.admission)).decode()  # type: ignore[arg-type,return-value]
    assert 'digitalizer_mark_groups{band="hide",source="rule"} 1.0' in text
    assert 'digitalizer_mark_groups{band="review",source="decider"} 1.0' in text
    assert "digitalizer_mark_decider_calls_saved 4.0" in text   # 4 rule members + 1 - 1 asked
    assert 'digitalizer_marks{band="hide"} 4.0' in text


# ---------------------------------------------------------------- labelling (#7)
def test_scan_mark_labels_one_crop_per_group_without_the_verdict(tmp_path: Path) -> None:
    studio = _studio(tmp_path, vision=fakes.FakeDecider(answers={m.SCAN_MARK: ("stamp", 0.7)}))
    for leaf in range(4):
        studio.engines.marks[leaf] = [_mark(STAMP)]
    studio.engines.marks[1].append(_mark(STICKER, bbox=(100, 100, 150, 150)))
    studio.execute(RunKind.FULL)
    svc = _services(studio)

    out = labelling.sample(svc, "scan_mark", 10)
    assert (out.kind, out.added) == ("mark_group", 2)     # two groups, not five occurrences
    item = labelling.next_item(svc, "scan_mark", "jana")
    assert item is not None and item.image_url == f"/api/v1/labelling/items/{item.id}/image"
    shown = repr(item.model_dump())
    for leak in ("hide", "repeated", "decider", "rule", "0.7", "category", "band"):
        assert leak not in shown, leak                    # never the model's answer
    assert labelling.item_image(svc, item.id).content[:4] == b"\x89PNG"
    labelling.submit(svc, item.id, labelling.LabelsIn(labels=[
        labelling.LabelIn(task="scan_mark", value="stamp")]), "jana")
    (line,) = list(labelling.export_lines(svc, "scan_mark"))
    assert line["task"] == "scan_mark" and line["label"] == "stamp"
    assert set(line["item"]) == {"book_id", "item_id", "group_id", "leaf", "bbox"}
    assert labelling.summary(svc, "scan_mark").labels == 1
    assert labelling.sample(svc, "scan_mark", 10).added == 0   # already sampled
