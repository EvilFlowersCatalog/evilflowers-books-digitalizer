"""§4: OCR-text metadata extraction, ISBN lookup and the merge into book metadata."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evilflowers_books_digitalizer.metadata.models import BookMetadata
from evilflowers_books_digitalizer.metadata.ocr_extract import (
    extract,
    find_isbns,
    find_year,
    select_pages,
)
from evilflowers_books_digitalizer.pipeline.base import BookContext
from evilflowers_books_digitalizer.pipeline.steps.extract_metadata import ExtractMetadata
from evilflowers_books_digitalizer.pipeline.steps.manifest import WriteCatalogManifest

ISBN13 = "9788022750462"  # = ISBN-10 80-227-5046-8
OTHER10 = "80-7095-020-X"  # a different valid ISBN-10
OTHER13 = "9788070950203"

TITLE_PAGE = """SLOVENSKÁ TECHNICKÁ UNIVERZITA V BRATISLAVE
Stavebná fakulta
Doc. Ing. Ján Novák, PhD.
BETÓNOVÉ KONŠTRUKCIE
Bratislava 2005"""

# a typical tiráž on the title-page verso (with realistic OCR artefacts)
COLOPHON = """Recenzenti: prof. Ing. Eva Malá, CSc.
Vydala Slovenská technická univerzita v Bratislave vo Vydavatel'stve STU,
Bratislava, Vazovova 5, v roku 2005.
Rozsah 210 strán, 150 obrázkov, 15,2 AH, 1. vydanie, edičné číslo 4512,
tlačilo Vydavateľstvo STU v Bratislave.
© Ján Novák, 2005
ISBN 80-227-5046-8"""

BACK_MATTER = """Literatúra
[1] Kováč, P.: Statika stavieb. Bratislava, Alfa 1996. ISBN 80-7095-020-X
[2] Eurokód 2 (STN EN 1992-1-1), 2006"""


def _pages(*texts: str) -> dict[int, str]:
    return dict(enumerate(texts))


# -- extraction -------------------------------------------------------------------


def test_slovak_colophon_fixture():
    result = extract(_pages(TITLE_PAGE, COLOPHON, "Obsah\n1 Úvod", BACK_MATTER))
    assert result.colophon_page == 1
    assert result.isbn == ISBN13
    assert result.year == 2005
    assert result.publisher == "Vydavateľstvo STU"
    assert result.isbns[0].colophon and result.isbns[0].labelled


def test_isbn10_with_bad_checksum_is_rejected():
    assert find_isbns(_pages("ISBN 80-227-5046-1")) == []
    assert extract(_pages("ISBN 80-227-5046-1")).isbn is None


@pytest.mark.parametrize("printed", [
    "ISBN 978-8O-227-5O46-2",               # O for 0
    "lSBN: 978 80 227 5046 2",              # l for I in the label
    "ISBN-13: 978-80-227-5046-2 (brož.)",   # -13 label, trailing binding note
    "brož. 978-80-227-5046-2",              # unlabelled 978 run
])
def test_isbn13_with_ocr_noise(printed):
    assert extract(_pages(printed)).isbn == ISBN13


def test_noise_fix_never_invents_a_valid_isbn():
    # 'l' -> '1' turns ...227... into ...221..., which fails the checksum
    assert extract(_pages("ISBN 978-80-22l-5046-2")).isbn is None


def test_l_and_i_for_1_inside_digits_are_repaired():
    # 978-80-227-1234-7 printed with OCR's 'l' and 'I' for the two 1s
    assert extract(_pages("ISBN 978-80-227-l234-7")).isbn == "9788022712347"
    assert extract(_pages("ISBN 978-80-227-I234-7")).isbn == "9788022712347"


def test_isbn10_converts_to_isbn13():
    assert extract(_pages("ISBN 80-227-5046-8")).isbn == ISBN13
    assert extract(_pages(f"ISBN {OTHER10}")).isbn == OTHER13


def test_two_candidates_prefer_the_colophon_page():
    result = extract(_pages(TITLE_PAGE, COLOPHON, BACK_MATTER))
    assert [c.isbn13 for c in result.isbns] == [ISBN13, OTHER13]
    # ...the reference on the last page loses even though it comes in any order
    result = extract({0: BACK_MATTER, 7: COLOPHON})
    assert result.isbn == ISBN13


def test_two_candidates_prefer_the_one_matching_the_directory_isbn():
    result = extract(_pages(COLOPHON, BACK_MATTER), known_isbn="807095020X")
    assert result.isbn == OTHER13


def test_year_is_clamped_and_falls_back_to_imprint():
    assert find_year(["© 2099 Future Press\nBratislava, vydanie 1998"], max_year=2026) == 1998
    assert find_year(["Copyright 1750"], max_year=2026) is None
    assert find_year(["Vydavateľstvo STU, Bratislava 2003"], max_year=2026) == 2003
    # digits inside an ISBN are not a year
    assert find_year(["vydanie: ISBN 80-227-1999-5"], max_year=2026) is None


def test_select_pages_head_and_tail_without_overlap():
    texts = [f"p{i}" for i in range(10)]
    assert list(select_pages(texts, 3, 2)) == [0, 1, 2, 8, 9]
    assert list(select_pages(texts[:3], 8, 4)) == [0, 1, 2]


# -- the pipeline step ------------------------------------------------------------


class FakeEnricher:
    def __init__(self, result: dict[str, Any] | None = None, error: Exception | None = None):
        self.result = result or {}
        self.error = error
        self.calls: list[str] = []

    def lookup(self, isbn: str) -> dict[str, Any]:
        self.calls.append(isbn)
        if self.error:
            raise self.error
        return dict(self.result)


OPAC = {
    "title": "Betónové konštrukcie I", "subtitle": "Navrhovanie",
    "authors": ["Novák, Ján"], "publisher": "STU", "year": 2004, "_source": "STU IPAC",
}


def _ctx(tmp_path: Path, pages: list[str], book_id: str = "CVI_OPACID_SVF_Betonove_konstrukcie",
         record: BookMetadata | None = None) -> BookContext:
    text = tmp_path / f"svf_{book_id}.txt"
    text.write_text("\f".join(pages), encoding="utf-8")
    ctx = BookContext(source="svf", book_id=book_id, work_dir=tmp_path, output_dir=tmp_path,
                      artifacts={"text": text})
    if record is not None:
        ctx.metadata["book_metadata"] = record.model_dump()
        ctx.metadata["title"] = record.title
    return ctx


def _step(enricher: FakeEnricher | None = None, **kwargs: Any) -> ExtractMetadata:
    return ExtractMetadata(enricher=enricher or FakeEnricher(), head_pages=8, tail_pages=4,
                           faculty_map={"svf": "Stavebná fakulta STU"}, **kwargs)


def test_stub_book_gets_ocr_and_opac_metadata(tmp_path):
    enricher = FakeEnricher(OPAC)
    ctx = _step(enricher).run(_ctx(tmp_path, [TITLE_PAGE, COLOPHON, BACK_MATTER]))

    assert enricher.calls == [ISBN13]
    meta = BookMetadata(**ctx.metadata["book_metadata"])
    assert meta.matched is True  # OPAC data: no longer a stub
    assert meta.title == "Betónové konštrukcie I"  # replaces the dirname title
    assert meta.isbn == ISBN13 and meta.year == 2004 and meta.publisher == "STU"
    assert meta.faculty == "Stavebná fakulta STU"
    assert meta.extra["provenance"] == {
        "title": "stu_opac", "subtitle": "stu_opac", "authors": "stu_opac",
        "year": "stu_opac", "publisher": "stu_opac", "isbn": "ocr",
    }
    assert meta.extra["ocr_candidates"]["year"] == 2005
    # the flat keys RenderPdf / EnrichPdfMetadata / GenerateCover read
    assert ctx.metadata["title"] == "Betónové konštrukcie I"
    assert ctx.metadata["authors"] == ["Novák, Ján"]
    assert ctx.metadata["isbn"] == ISBN13
    assert ctx.metadata["catalog_matched"] is True


def test_merge_precedence_excel_then_lookup_then_ocr(tmp_path):
    excel = BookMetadata(book_id="b", title="Excel title", publisher="Excel pub",
                         faculty="SvF", matched=True)
    enricher = FakeEnricher({**OPAC, "publisher": None})
    ctx = _step(enricher).run(_ctx(tmp_path, [COLOPHON], book_id="b", record=excel))
    meta = BookMetadata(**ctx.metadata["book_metadata"])
    prov = meta.extra["provenance"]
    assert (meta.title, prov["title"]) == ("Excel title", "excel")
    assert (meta.publisher, prov["publisher"]) == ("Excel pub", "excel")
    assert (meta.authors, prov["authors"]) == (["Novák, Ján"], "stu_opac")  # excel was empty
    assert (meta.year, prov["year"]) == (2004, "stu_opac")  # opac beats ocr (2005)
    assert (meta.isbn, prov["isbn"]) == (ISBN13, "ocr")


def test_ocr_fills_what_nothing_else_has(tmp_path):
    ctx = _step(FakeEnricher({})).run(_ctx(tmp_path, [TITLE_PAGE, COLOPHON]))
    meta = BookMetadata(**ctx.metadata["book_metadata"])
    assert meta.matched is False  # no lookup hit: still a stub
    assert meta.title == "Betonove Konstrukcie"
    assert meta.extra["provenance"]["title"] == "dirname"
    assert (meta.year, meta.publisher, meta.isbn) == (2005, "Vydavateľstvo STU", ISBN13)
    assert {meta.extra["provenance"][f] for f in ("year", "publisher", "isbn")} == {"ocr"}


def test_override_lets_the_lookup_replace_excel(tmp_path):
    excel = BookMetadata(book_id="b", title="Excel title", isbn=ISBN13, matched=True)
    ctx = _step(FakeEnricher(OPAC), override=True).run(
        _ctx(tmp_path, [COLOPHON], book_id="b", record=excel))
    meta = BookMetadata(**ctx.metadata["book_metadata"])
    assert meta.title == "Betónové konštrukcie I"
    assert meta.extra["provenance"]["title"] == "stu_opac"
    assert meta.extra["provenance"]["isbn"] == "excel"  # the lookup brings no ISBN


def test_lookup_uses_the_excel_isbn_first(tmp_path):
    excel = BookMetadata(book_id="b", title="t", isbn=OTHER10, matched=True)
    enricher = FakeEnricher({})
    _step(enricher).run(_ctx(tmp_path, [COLOPHON], book_id="b", record=excel))
    assert enricher.calls == ["807095020X"]


def test_network_error_is_a_warning_not_a_failure(tmp_path, caplog):
    enricher = FakeEnricher(error=OSError("network unreachable"))
    with caplog.at_level("WARNING"):
        ctx = _step(enricher).run(_ctx(tmp_path, [COLOPHON]))
    meta = BookMetadata(**ctx.metadata["book_metadata"])
    assert meta.isbn == ISBN13 and meta.year == 2005  # OCR results kept
    assert any("network unreachable" in r.getMessage() and r.levelname == "WARNING"
               for r in caplog.records)


def test_lookup_disabled_never_calls_the_enricher(tmp_path):
    enricher = FakeEnricher(OPAC)
    _step(enricher, lookup=False).run(_ctx(tmp_path, [COLOPHON]))
    assert enricher.calls == []


def test_missing_text_still_yields_a_stub(tmp_path):
    ctx = BookContext(source="svf", book_id="FEI_9788022750462", work_dir=tmp_path,
                      output_dir=tmp_path)
    enricher = FakeEnricher({})
    ctx = _step(enricher).run(ctx)
    meta = BookMetadata(**ctx.metadata["book_metadata"])
    assert meta.isbn == ISBN13 and meta.extra["provenance"]["isbn"] == "dirname"
    assert enricher.calls == [ISBN13]  # the directory ISBN is still worth a lookup


def test_unexpected_failure_never_stops_the_book(tmp_path, monkeypatch):
    step = _step()
    monkeypatch.setattr(step, "_run", lambda ctx: (_ for _ in ()).throw(KeyError("x")))
    ctx = _ctx(tmp_path, [COLOPHON])
    assert step.run(ctx) is ctx


def test_enrichment_reaches_the_catalog_manifest(tmp_path):
    ctx = _step(FakeEnricher(OPAC)).run(_ctx(tmp_path, [TITLE_PAGE, COLOPHON]))
    pdf = tmp_path / f"{ctx.slug}.pdf"
    pdf.write_bytes(b"%PDF")
    ctx.artifacts["pdf"] = pdf
    ctx.metadata["ocr_language"] = "slk+eng"
    WriteCatalogManifest("", by_source=True).run(ctx)

    from evilflowers_books_digitalizer.catalog.manifest import read_manifest

    manifest = read_manifest(ctx.artifacts["manifest"])
    assert manifest.identifiers == {"isbn": ISBN13}
    assert manifest.publisher == "STU" and manifest.published_at == "2004"
    assert manifest.catalog == "svf" and manifest.language_code == "sk"
    assert manifest.provenance["title"] == "stu_opac"
    assert manifest.ocr_candidates["publisher"] == "Vydavateľstvo STU"
    entry = manifest.to_entry_create().model_dump()
    assert "provenance" not in entry and "ocr_candidates" not in entry
