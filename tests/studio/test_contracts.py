"""Studio contract: domain rules, IA metadata semantics, config identifiers, events."""

from __future__ import annotations

from pathlib import Path

import pytest

from evilflowers_books_digitalizer.studio.config import ProjectConfig
from evilflowers_books_digitalizer.studio.domain import (
    BBox,
    BookStatus,
    PageOverride,
    QualitySignals,
    RegionAction,
    RegionKind,
    RegionProposal,
    RegionSource,
    RegionStatus,
    TransitionError,
    check_transition,
    fuse,
    quality_gate,
)
from evilflowers_books_digitalizer.studio.events import Event, EventType
from evilflowers_books_digitalizer.studio.metadata import (
    ItemMetadata,
    Leaf,
    PageType,
    access_index,
    default_add_to_access,
    marc_language,
)


def P(kind, box, source=RegionSource.HEURISTIC, conf=0.9):  # noqa: N802
    return RegionProposal(RegionKind(kind), BBox(*box), source, conf)


# ---------------------------------------------------------------- fusion
def test_agreeing_detectors_merge_into_one_proposed_region():
    out = fuse([P("text", (0, 0, 100, 50)), P("list_item", (2, 0, 100, 52), RegionSource.YOLO, 0.8)])
    assert len(out) == 1 and out[0].status == RegionStatus.PROPOSED   # text ~ list_item


def test_conflicting_detectors_are_disputed():
    out = fuse([P("formula", (0, 0, 100, 50), RegionSource.YOLO, 0.7),
                P("picture", (0, 0, 98, 50), RegionSource.HEURISTIC, 0.6)])
    assert out[0].status == RegionStatus.DISPUTED and RegionKind.PICTURE in out[0].alternatives
    assert out[0].action == RegionAction.REOCR


def test_low_confidence_and_stamps_need_a_person():
    out = fuse([P("table", (0, 0, 100, 50), conf=0.3), P("stamp", (0, 200, 80, 280), conf=0.99)])
    assert [r.status for r in out] == [RegionStatus.DISPUTED, RegionStatus.DISPUTED]
    assert out[1].action == RegionAction.REMOVE


def test_operator_wins_and_is_confirmed():
    out = fuse([P("picture", (0, 0, 100, 50), RegionSource.YOLO, 0.99),
                P("formula", (1, 1, 99, 49), RegionSource.OPERATOR, 0.1)])
    assert len(out) == 1 and out[0].kind == RegionKind.FORMULA
    assert out[0].status == RegionStatus.CONFIRMED and out[0].source == RegionSource.OPERATOR


def test_bbox_validation_and_iou():
    with pytest.raises(ValueError):
        BBox(10, 10, 5, 20)
    assert BBox(0, 0, 10, 10).iou(BBox(5, 0, 15, 10)) == pytest.approx(1 / 3)


# ---------------------------------------------------------------- gate + lifecycle
def test_quality_gate():
    clean = quality_gate(QualitySignals(pages=10, ocr_mean_conf=92, disputed_regions=0,
                                        formula_fallbacks=1, vlm_rejected=0), formulas=10)
    assert clean.passed and clean.score > 0.9
    bad = quality_gate(QualitySignals(pages=10, ocr_mean_conf=60, disputed_regions=3,
                                      formula_fallbacks=8, vlm_rejected=5, foreign_letters=2),
                       formulas=10)
    assert not bad.passed and len(bad.flags) == 5 and bad.score < clean.score
    signals = bad.as_dict()["signals"]
    assert isinstance(signals, dict) and signals["disputed_regions"] == 3


def test_book_transitions():
    assert check_transition("review", "approved") == BookStatus.APPROVED
    with pytest.raises(TransitionError):
        check_transition("discovered", "published")


def test_page_override_round_trip():
    o = PageOverride(crop=BBox(10, 20, 900, 1200), deskew_deg=-0.4, despeckle="normal")
    assert PageOverride.from_dict(o.as_dict()) == o
    assert PageOverride.from_dict(None) == PageOverride()


# ---------------------------------------------------------------- IA semantics
def test_item_metadata_ia_round_trip():
    ia = {"identifier": "stu-fei-9788022726634", "mediatype": "texts", "title": "Vektory",
          "creator": ["Červeň, Ivan"], "language": "slo", "oclc-id": ["123"],
          "page-progression": "lr", "ppi": "300", "description": ["a", "b"],
          "evilflowers_faculty": "fei"}
    m = ItemMetadata.from_ia(ia)
    assert m.language == ["slo"] and m.oclc_id == ["123"] and m.ppi == 300
    assert m.extra == {"evilflowers_faculty": ["fei"]}
    back = m.to_ia()
    assert back["oclc-id"] == ["123"] and back["page-progression"] == "lr"
    assert back["evilflowers_faculty"] == "fei" and back["language"] == ["slo"]
    assert ItemMetadata.from_dict(m.as_dict()) == m


def test_metadata_merge_fills_gaps_only():
    a = ItemMetadata("item-a", title="Kept")
    a.merge(ItemMetadata("item-b", title="Ignored", publisher="Bratislava : STU", isbn=["978"]))
    assert (a.title, a.publisher, a.isbn, a.identifier) == ("Kept", "Bratislava : STU", ["978"], "item-a")


def test_leaves_and_access_index():
    assert marc_language("slk+eng") == "slo"
    assert not default_add_to_access(PageType.COLOR_CARD) and default_add_to_access(PageType.COVER)
    leaves = [Leaf(0, PageType.COLOR_CARD, add_to_access=False), Leaf(1, PageType.COVER),
              Leaf(2), Leaf(3, PageType.DELETE, add_to_access=False), Leaf(4, page_number="1")]
    assert access_index(leaves) == {1: 0, 2: 1, 4: 2}


@pytest.mark.parametrize(("template", "expected"), [
    ("stu-{collection}-{book}", "stu-fei-cvi_opacid_fei_9788022726634"),
    ("{book}", "ab_item"),
    ("--{book}--", "ab_item"),
])
def test_item_identifier_is_ia_compatible(template, expected):
    book = "CVI_OPACID_FEI_9788022726634" if "collection" in template else "AB"
    p = ProjectConfig(slug="stu", name="STU", scans_root=Path("/x"), item_id=template)
    ident = p.item_identifier("fei", book)
    assert ident == expected and 5 <= len(ident) <= 80 and ident[0].isalnum()


# ---------------------------------------------------------------- events
def test_event_round_trip_and_filters():
    e = Event(EventType.PAGE_UPDATED, "stu", 7, {"index": 3, "version": 2})
    back = Event.from_json(e.to_json())
    assert back == e
    assert e.matches() and e.matches(project="stu") and not e.matches(project="ku")
    assert e.matches(book_id=7) and not e.matches(book_id=8)
