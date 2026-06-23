"""Map BookMetadata + artifacts onto a catalog entry manifest."""

from __future__ import annotations

from pathlib import Path

from evilflowers_books_digitalizer.catalog.mapping import (
    EntryAuthor,
    build_manifest,
    resolve_language,
    split_author,
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
    assert resolve_language(meta, ocr_language="slk+eng") == "slk"
    assert resolve_language(meta, default="ces") == "ces"


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

    payload = manifest.entry_payload(image_data_uri="data:image/jpeg;base64,QQ==")
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
    assert manifest.language_code == "slk"
    assert "identifiers" not in manifest.entry_payload()
