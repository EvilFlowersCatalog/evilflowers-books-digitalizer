"""`ia upload`-compatible IA-S3 metadata headers."""

from __future__ import annotations

from evilflowers_books_digitalizer.studio.adapters.ia.headers import (
    decode_value,
    meta_headers,
    upload_headers,
)
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata


def test_meta_headers_follow_the_internetarchive_rules():
    meta = ItemMetadata("stu-fei-x1", title="Vektory", creator=["Červeň, Ivan"],
                        subject=["fyzika", "matematika"], language=["slo"], date="2007",
                        extra={"evilflowers_faculty": ["fei"]})
    h = meta_headers(meta)
    assert "x-archive-meta-identifier" not in h
    assert h["x-archive-meta-mediatype"] == "texts"
    assert h["x-archive-meta-title"] == "Vektory"
    assert h["x-archive-meta01-subject"] == "fyzika" and h["x-archive-meta02-subject"] == "matematika"
    assert h["x-archive-meta-creator"].startswith("uri(") and \
        decode_value(h["x-archive-meta-creator"]) == "Červeň, Ivan"
    assert h["x-archive-meta-evilflowers--faculty"] == "fei"      # _ -> -- (IA-S3 maps it back)
    assert h["x-archive-meta-page-progression"] == "lr"
    assert all(v.isascii() for v in h.values())


def test_upload_headers_disable_derive_by_default():
    h = upload_headers(ItemMetadata("stu-fei-x1", title="T"), size_hint=10)
    assert h["x-archive-queue-derive"] == "0" and h["x-archive-auto-make-bucket"] == "1"
    assert h["x-archive-size-hint"] == "10"
    assert "x-archive-queue-derive" not in upload_headers(ItemMetadata("stu-fei-x1"), queue_derive=True)
