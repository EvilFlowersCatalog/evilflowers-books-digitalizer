"""Title page -> title, authors, publisher, year suggestions (evilflowers-books-digitalizer#15).

After the analyze stage the orchestrator's ``analyze/title_page`` step reads the first and last
leaves' hOCR, ranks line candidates per field and asks the text decider one ``choice`` per
field. The suggestions prefill the operator's form; only a confirmation writes the metadata,
and each confirmed field is a labelled ``title_page`` case (#7).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from batch_harness import PROJECT, Studio, make_studio

from evilflowers_books_digitalizer.studio import title_page as tp
from evilflowers_books_digitalizer.studio.adapters.queue import RecordingTaskQueue
from evilflowers_books_digitalizer.studio.application import labelling
from evilflowers_books_digitalizer.studio.application import title_page as uc
from evilflowers_books_digitalizer.studio.application.errors import BadRequest, Invalid
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.config import TitlePageConfig
from evilflowers_books_digitalizer.studio.domain import RunKind, RunStatus
from evilflowers_books_digitalizer.studio.hocr import parse_pages
from evilflowers_books_digitalizer.studio.testing import fakes
from evilflowers_books_digitalizer.studio.testing.engines import OcrPage

#: leaf -> (text, size, x0, x1) lines of a 200 px wide page
LINES: dict[int, list[tuple[str, int, int, int]]] = {
    0: [("Ján Novák, Peter Kováč", 22, 40, 160), ("Fyzika pre inžinierov", 40, 20, 180),
        ("Bratislava", 14, 70, 130)],
    1: [("© Ján Novák, 2011", 12, 10, 120), ("Vydavateľstvo STU, Bratislava 2011", 12, 10, 190),
        ("ISBN 978-80-227-3504-9", 12, 10, 150)],
    2: [("Obsah strany dva", 12, 10, 190)],
    3: [("Koniec knihy", 12, 10, 190)],
}


class TitlePageOcr(fakes.FakeOcr):
    """ocr-service reading the synthetic book: real lines with sizes and positions per leaf."""

    def recognize(self, image: Path, *, language: str, dpi: int) -> OcrPage:
        leaf = int(image.stem)
        rows, y = [], 10
        for n, (text, size, x0, x1) in enumerate(LINES.get(leaf, [])):
            words = "".join(f"<span class='ocrx_word' title='bbox {x0} {y} {x1} {y + size}; "
                            f"x_wconf 90'>{w}</span> " for w in text.split())
            rows.append(f"<span class='ocr_line' id='line_1_{n}' title='bbox {x0} {y} {x1} "
                        f"{y + size}; x_size {size}'>{words}</span>")
            y += size + 8
        hocr = (f"<div class='ocr_page' title='bbox 0 0 200 300'><p class='ocr_par' "
                f"title='bbox 0 0 200 300'>{''.join(rows)}</p></div>")
        return OcrPage(hocr, "\n".join(t for t, *_ in LINES.get(leaf, [])), 90.0)


def _decider(**kw: Any) -> fakes.FakeDecider:
    return fakes.FakeDecider(by_text={"title_page_title": "Fyzika",
                                      "title_page_authors": "Novák, Peter",
                                      "title_page_publisher": "Vydavateľstvo",
                                      "title_page_year": "©"}, **kw)


def _studio(tmp_path: Path, *, text: Any = None, **config: Any) -> Studio:
    studio = make_studio(tmp_path, deciders={"text": text, "vision": None}, **config)
    studio.engines.ocr = TitlePageOcr()
    tasks().discover.delay(PROJECT)
    return studio


def tasks() -> Any:
    from evilflowers_books_digitalizer.studio.workers import tasks as t

    return t


def _services(studio: Studio) -> Services:
    return Services(config=studio.ctx.config, sessions=studio.ctx.session_factory,
                    queue=RecordingTaskQueue(), bus=studio.bus)


# ---------------------------------------------------------------- pure candidates
def test_candidates_are_ranked_per_field() -> None:
    page = parse_pages(TitlePageOcr().recognize(Path("0000.tif"), language="slk", dpi=300).hocr)[0]
    verso = parse_pages(TitlePageOcr().recognize(Path("0001.tif"), language="slk", dpi=300).hocr)[0]
    lines = tp.lines_of(0, page) + tp.lines_of(1, verso, first_order=10)
    first = {0, 1}
    assert tp.rank("title", lines, first, 3)[0].text == "Fyzika pre inžinierov"
    assert tp.rank("authors", lines, first, 3)[0].text == "Ján Novák, Peter Kováč"
    assert tp.rank("publisher", lines, first, 3)[0].text.startswith("Vydavateľstvo STU")
    assert {c.text for c in tp.rank("year", lines, first, 3)} == {
        "© Ján Novák, 2011", "Vydavateľstvo STU, Bratislava 2011"}
    assert tp.value("year", "© Ján Novák, 2011") == "2011"
    assert tp.value("authors", "Ján Novák a Peter Kováč") == ["Ján Novák", "Peter Kováč"]
    assert list(tp.criteria(tp.rank("title", lines, first, 2)))[-1] == "none"
    assert "[L0-1] (size 40) Fyzika pre inžinierov" in tp.state(lines)


# ---------------------------------------------------------------- end to end
def test_a_full_run_stores_suggestions_and_imports_nothing(tmp_path: Path) -> None:
    decider = _decider()
    studio = _studio(tmp_path, text=decider)
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error

    book = studio.book()
    s = book.metadata_suggestions or {}
    assert s["title"]["value"] == "Fyzika pre inžinierov" and s["title"]["p"] == 0.9
    assert s["authors"]["value"] == ["Ján Novák", "Peter Kováč"]
    assert s["publisher"]["value"] == "Vydavateľstvo STU, Bratislava 2011"
    assert (s["year"]["value"], s["year"]["leaf"]) == ("2011", 1)
    assert s["decider_model"] == "fake-decider@1"
    assert book.item_metadata["title"] == "Testovacia kniha"     # never imported unconfirmed
    assert [c["question"] for c in decider.calls] == [
        "title_page_title", "title_page_authors", "title_page_publisher", "title_page_year"]
    assert all(set(c["criteria"]) >= {"none"} and "body" in c["state"] for c in decider.calls)


def test_without_a_decider_or_when_it_fails_nothing_is_suggested(tmp_path: Path) -> None:
    off = _studio(tmp_path / "off")
    assert off.execute(RunKind.FULL).status == RunStatus.SUCCEEDED
    assert off.book().metadata_suggestions is None
    down = _studio(tmp_path / "down", text=_decider(fail=True))
    run = down.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED
    assert down.book().metadata_suggestions is None
    assert "decider failed for title, authors, publisher, year" in str(
        down.session().get(type(run), run.id).log)


def test_a_rerun_of_a_middle_page_does_not_ask_again(tmp_path: Path) -> None:
    decider = _decider()
    studio = _studio(tmp_path, text=decider, title_page=TitlePageConfig(first_leaves=2,
                                                                        last_leaves=1))
    studio.execute(RunKind.FULL)
    asked = len(decider.calls)
    studio.execute(RunKind.RERUN, pages=[2])
    assert len(decider.calls) == asked
    studio.execute(RunKind.RERUN, pages=[0])
    assert len(decider.calls) == 2 * asked


# ---------------------------------------------------------------- the operator's confirmation
def test_confirming_writes_the_metadata_and_records_labelled_cases(tmp_path: Path) -> None:
    studio = _studio(tmp_path, text=_decider())
    studio.execute(RunKind.FULL)
    svc = _services(studio)
    book_id = studio.book().id
    shown = uc.get(svc, book_id)
    assert [(s.field, s.metadata_field) for s in shown.suggestions] == [
        ("title", "title"), ("authors", "creator"), ("publisher", "publisher"), ("year", "date")]

    out = uc.confirm(svc, book_id, uc.ConfirmIn(
        title="Fyzika pre inžinierov II", authors=["Novák, Ján", "Kováč, Peter"],
        publisher=None, year="2011"), "jana")
    cases = {k: v["outcome"] for k, v in (out.confirmed or {})["fields"].items()}
    assert cases == {"title": "changed",          # "II" is not on the chosen line
                     "authors": "accepted",       # reformatted to IA's "Surname, Given"
                     "publisher": "none", "year": "accepted"}
    meta = studio.book().item_metadata
    assert (meta["title"], meta["creator"], meta["date"]) == (
        "Fyzika pre inžinierov II", ["Novák, Ján", "Kováč, Peter"], "2011")
    assert studio.book().title == "Fyzika pre inžinierov II"

    lines = list(labelling.export_lines(svc, "title_page"))
    assert [(ln["item"]["field"], ln["label"]) for ln in lines] == [
        ("title", "changed"), ("authors", "accepted"), ("publisher", "none"), ("year", "accepted")]
    assert lines[0]["p"] == 0.9 and lines[0]["labeller"] == "jana"
    assert lines[0]["decider_model"] == "fake-decider@1" and lines[0]["language"] == "slo"
    summary = labelling.summary(svc, "title_page")
    assert summary.labels == 4 and summary.by_label["title/changed"] == 1
    tasks_out = {t.name: t for t in labelling.list_tasks(svc, "jana")}
    assert (tasks_out["title_page"].items, tasks_out["title_page"].labelled) == (1, 4)
    with pytest.raises(BadRequest):
        labelling.next_item(svc, "title_page", "jana")       # nothing to answer on a screen
    with pytest.raises(BadRequest):
        labelling.sample(svc, "title_page", 5)

    studio.execute(RunKind.RERUN, pages=[0])                 # confirmed: never re-suggested
    assert studio.book().metadata_confirmed is not None


def test_confirming_validates_and_needs_suggestions(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    studio.execute(RunKind.FULL)
    svc = _services(studio)
    with pytest.raises(BadRequest, match="no title-page suggestions"):
        uc.confirm(svc, studio.book().id, uc.ConfirmIn(title="x"), "jana")
    with_decider = _studio(tmp_path / "b", text=_decider())
    with_decider.execute(RunKind.FULL)
    with pytest.raises(Invalid, match="date"):
        uc.confirm(_services(with_decider), with_decider.book().id,
                   uc.ConfirmIn(year="around 2011"), "jana")


def test_metrics_count_confirmation_outcomes(tmp_path: Path) -> None:
    from evilflowers_books_digitalizer.studio import ledger_metrics

    studio = _studio(tmp_path, text=_decider())
    studio.execute(RunKind.FULL)
    collector = ledger_metrics.LedgerCollector(studio.ctx.session_factory,
                                               lambda: studio.ctx.admission)  # type: ignore[arg-type,return-value]
    assert 'digitalizer_title_page_fields{field="title",outcome="pending"} 1.0' in \
        ledger_metrics.render(collector).decode()
    uc.confirm(_services(studio), studio.book().id, uc.ConfirmIn(title="Iný názov"), "jana")
    text = ledger_metrics.render(collector).decode()
    assert 'digitalizer_title_page_fields{field="title",outcome="changed"} 1.0' in text
    assert 'digitalizer_title_page_fields{field="year",outcome="none"} 1.0' in text
