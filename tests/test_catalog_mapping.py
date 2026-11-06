"""Map BookMetadata + artifacts onto a catalog entry manifest."""

from __future__ import annotations

from pathlib import Path

import pytest
from evilflowers_protocol.forms import EntryCreate

from evilflowers_books_digitalizer.catalog.mapping import (
    EntryAuthor,
    EntryManifest,
    build_manifest,
    normalize_language,
    resolve_language,
    split_author,
    target_catalog,
)
from evilflowers_books_digitalizer.metadata.models import BookMetadata


def test_split_author_comma_form_is_unambiguous():
    assert split_author("Novák, Ján") == EntryAuthor(name="Ján", surname="Novák")


def test_split_author_given_first_default():
    assert split_author("Ján Novák") == EntryAuthor(name="Ján", surname="Novák")


def test_split_author_surname_first_order():
    assert split_author("Novák Ján", order="surname_first") == EntryAuthor(
        name="Ján", surname="Novák"
    )


def test_split_author_single_token_is_surname():
    assert split_author("Aristotle") == EntryAuthor(name="", surname="Aristotle")


def test_resolve_language_prefers_catalog_then_ocr_then_default():
    assert resolve_language(BookMetadata(book_id="b", title="t", language="en")) == "en"
    meta = BookMetadata(book_id="b", title="t")
    assert resolve_language(meta, ocr_language="slk+eng") == "sk"
    assert resolve_language(meta, default="ces") == "cs"
    assert resolve_language(meta) == "sk"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("sk", "sk"), ("SK", "sk"), ("slk", "sk"), ("slo", "sk"), ("slk+eng", "sk"),
        ("ces", "cs"), ("cze", "cs"), ("eng+slk", "en"), ("deu", "de"), ("sk-SK", "sk"),
        ("lat", "lat"),  # no alpha2 in our table -> alpha3 fallback
        ("", None), (None, None),
    ],
)
def test_normalize_language_is_iso639_1_with_alpha3_fallback(code, expected):
    assert normalize_language(code) == expected


def test_catalog_xlsx_language_alpha3_is_normalized():
    meta = BookMetadata(book_id="b", title="t", language="slk")
    manifest = build_manifest(meta, source="svf", book_id="b", pdf=Path("svf_b.pdf"), catalog="c")
    assert manifest.language_code == "sk"


def test_old_manifest_with_alpha3_sends_alpha2():
    manifest = EntryManifest(source="svf", book_id="b", slug="svf_b", catalog="svf",
                             title="T", language_code="slk", pdf="svf_b.pdf")
    assert manifest.to_entry_create().language_code == "sk"


def test_target_catalog_precedence():
    assert target_catalog("svf", override="special", by_source=True, default="d") == "special"
    assert target_catalog("svf", override=None, by_source=True, default="d") == "svf"
    assert target_catalog("svf", override=None, by_source=False, default="d") == "d"
    with pytest.raises(ValueError):
        target_catalog("svf", override=None, by_source=False, default="")


def test_build_manifest_by_source_and_override():
    meta = BookMetadata(book_id="b", title="t")
    manifest = build_manifest(meta, source="fei", book_id="b", pdf=Path("fei_b.pdf"),
                              catalog="", by_source=True)
    assert manifest.catalog == "fei"
    assert manifest.catalog_override is None
    meta = BookMetadata(book_id="b", title="t", catalog="mtf")
    manifest = build_manifest(meta, source="fei", book_id="b", pdf=Path("fei_b.pdf"),
                              catalog="", by_source=True)
    assert (manifest.catalog, manifest.catalog_override) == ("mtf", "mtf")


def test_to_entry_create_builds_protocol_form():
    manifest = EntryManifest(
        source="svf", book_id="b", slug="svf_b", catalog="svf",
        title="  " + "x" * 300, language_code="slk",
        authors=[EntryAuthor(name="Ján", surname="Novák"), EntryAuthor(name="", surname="STU")],
        publisher="Vydavateľstvo STU", published_at="2005",
        identifiers={"isbn": "9788022750462", "bogus": "1", "doi": ""},
        config={"evilflowers_ocr_enabled": False, "evilflowers_metadata_fetch": True},
        pdf="svf_b.pdf",
    )
    entry = manifest.to_entry_create(image="data:image/jpeg;base64,QQ==")
    assert isinstance(entry, EntryCreate)
    assert len(entry.title) == 255
    assert entry.language_code == "sk"
    # the catalog needs both name and surname: a single-token author is left out
    assert [(a.name, a.surname) for a in entry.authors] == [("Ján", "Novák")]
    assert entry.identifiers == {"isbn": "9788022750462"}
    assert entry.image == "data:image/jpeg;base64,QQ=="
    body = entry.model_dump()
    assert body["config"] == {"evilflowers_ocr_enabled": False, "evilflowers_metadata_fetch": True}
    assert body["published_at"] == "2005"


def test_isbn_identifier_is_compacted():
    meta = BookMetadata(book_id="b", title="t", isbn="978-80-227-5046-2")
    manifest = build_manifest(meta, source="svf", book_id="b", pdf=Path("p.pdf"), catalog="c")
    assert manifest.identifiers == {"isbn": "9788022750462"}


def test_provenance_travels_in_manifest_but_not_payload():
    meta = BookMetadata(book_id="b", title="t", extra={
        "provenance": {"title": "stu_opac"}, "ocr_candidates": {"isbns": []}})
    manifest = build_manifest(meta, source="svf", book_id="b", pdf=Path("p.pdf"), catalog="c")
    assert manifest.provenance == {"title": "stu_opac"}
    assert manifest.ocr_candidates == {"isbns": []}
    assert "provenance" not in manifest.to_entry_create().model_dump()


def test_build_manifest_maps_fields_and_payload():
    meta = BookMetadata(
        book_id="9788022750462",
        title="Betónové konštrukcie",
        authors=["Novák Ján", "Malá Eva"],
        year=2019,
        publisher="STU",
        isbn="9788022750462",
        faculty="SvF",
        language="sk",
    )
    manifest = build_manifest(
        meta,
        source="svf",
        book_id="CVI_OPACID_SVF_9788022750462",
        pdf=Path("/out/svf/svf_CVI_OPACID_SVF_9788022750462.pdf"),
        cover=Path("/out/svf/svf_CVI_OPACID_SVF_9788022750462.cover.jpg"),
        catalog="stu-books",
        default_language="slk",
        author_name_order="surname_first",
        entry_config={"evilflowers_ocr_enabled": False},
    )

    assert manifest.slug == "svf_CVI_OPACID_SVF_9788022750462"
    assert manifest.catalog == "stu-books"
    assert manifest.pdf == "svf_CVI_OPACID_SVF_9788022750462.pdf"  # basename only
    assert manifest.cover == "svf_CVI_OPACID_SVF_9788022750462.cover.jpg"
    assert manifest.identifiers == {"isbn": "9788022750462"}
    assert manifest.published_at == "2019"
    assert manifest.authors[0] == EntryAuthor(name="Ján", surname="Novák")

    payload = manifest.to_entry_create(image="data:image/jpeg;base64,QQ==").model_dump()
    assert payload["title"] == "Betónové konštrukcie"
    assert payload["language_code"] == "sk"
    assert payload["identifiers"] == {"isbn": "9788022750462"}
    assert payload["publisher"] == "STU"
    assert payload["image"].startswith("data:image/jpeg;base64,")
    assert payload["authors"][1] == {"name": "Eva", "surname": "Malá"}


def test_per_book_catalog_override_wins():
    meta = BookMetadata(book_id="b", title="t", catalog="special-catalog")
    manifest = build_manifest(
        meta, source="fei", book_id="b", pdf=Path("fei_b.pdf"), catalog="stu-books"
    )
    assert manifest.catalog == "special-catalog"


def test_build_manifest_without_isbn_has_no_identifiers():
    meta = BookMetadata(book_id="b", title="Untitled")
    manifest = build_manifest(meta, source="fad", book_id="b", pdf=Path("fad_b.pdf"),
                              catalog="c", default_language="slk")
    assert manifest.identifiers == {}
    assert manifest.language_code == "sk"
    assert "identifiers" not in manifest.to_entry_create().model_dump()
