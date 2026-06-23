"""Catalog matching, id normalization and graceful fallbacks."""

from __future__ import annotations

import pandas as pd

from evilflowers_books_digitalizer.metadata import (
    BookMetadata,
    MetadataCatalog,
    deslug,
    extract_dir_id,
    normalize_id,
)


def test_extract_dir_id_strips_prefix_and_faculty():
    assert extract_dir_id("CVI_OPACID_FA_9788022750462") == "9788022750462"
    assert extract_dir_id("FEI_9788089422012") == "9788089422012"
    assert (
        extract_dir_id("CVI_OPACID_FA_Architektonicka_kompozicia") == "Architektonicka_kompozicia"
    )


def test_normalize_id_collapses_isbn_punctuation_and_check_digit():
    assert normalize_id("978-80-227-5046-2") == normalize_id("9788022750462")
    assert normalize_id("807095020_X") == "807095020X"


def test_deslug_humanizes_directory_name():
    assert deslug("CVI_OPACID_FA_Architektonicka_kompozicia") == "Architektonicka Kompozicia"


def test_lookup_matches_by_isbn_with_punctuation():
    recs = [BookMetadata(book_id="x", title="Pozemné staviteľstvo", isbn="978-80-227-5046-2")]
    cat = MetadataCatalog(recs, key_field="isbn")
    found = cat.lookup("CVI_OPACID_FA_9788022750462")
    assert found.matched is True
    assert found.title == "Pozemné staviteľstvo"
    assert found.book_id == "CVI_OPACID_FA_9788022750462"  # rebound to the queried dir


def test_lookup_unmatched_returns_stub():
    cat = MetadataCatalog([], key_field="isbn")
    stub = cat.lookup("CVI_OPACID_FA_Nieco_Ine", faculty="FA")
    assert stub.matched is False
    assert stub.title == "Nieco Ine"
    assert stub.faculty == "FA"


def test_from_excel_splits_authors_and_coerces_year(tmp_path):
    path = tmp_path / "catalog.xlsx"
    pd.DataFrame(
        [
            {
                "ISBN": "978-80-227-5046-2",
                "Názov": "Kniha",
                "Autor": "A Nový; B Malá",
                "Rok": "vyd. 2009",
            },
            {"ISBN": None, "Názov": None, "Autor": None, "Rok": None},  # blank row dropped
        ]
    ).to_excel(path, index=False)

    cat = MetadataCatalog.from_excel(
        path,
        columns={"title": "Názov", "authors": "Autor", "year": "Rok", "isbn": "ISBN"},
        key_field="isbn",
    )
    assert len(cat) == 1
    rec = cat.lookup("CVI_OPACID_FA_9788022750462")
    assert rec.authors == ["A Nový", "B Malá"]
    assert rec.year == 2009


def test_match_report_counts():
    cat = MetadataCatalog([BookMetadata(book_id="x", title="T", isbn="9788022750462")])
    report = cat.match_report(["CVI_OPACID_FA_9788022750462", "CVI_OPACID_FA_Missing"])
    assert report == {
        "books": 2,
        "matched": 1,
        "missed": 1,
        "rows": 1,
        "miss_sample": ["CVI_OPACID_FA_Missing"],
    }
