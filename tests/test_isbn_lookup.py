"""ISBN validation, provider parsing (network mocked), caching, draft enrichment."""

from __future__ import annotations

import json

from evilflowers_books_digitalizer.metadata import isbn_lookup as il
from evilflowers_books_digitalizer.metadata.draft import HEADERS, DraftBook, build_draft_rows


def test_is_valid_isbn():
    assert il.is_valid_isbn("0415128269")  # isbn-10
    assert il.is_valid_isbn("3-8228-1162-9")  # isbn-10 with punctuation
    assert il.is_valid_isbn("9780500203958")  # isbn-13
    assert not il.is_valid_isbn("0415128260")  # bad isbn-10 check digit
    assert not il.is_valid_isbn("12345")  # wrong length
    assert not il.is_valid_isbn("Betonove")


def test_openlibrary_parsing(monkeypatch):
    payload = {
        "ISBN:0415128269": {
            "title": "Rethinking Architecture",
            "authors": [{"name": "Neil Leach"}],
            "publishers": [{"name": "Routledge"}],
            "publish_date": "1997",
        }
    }
    monkeypatch.setattr(il, "_get_json", lambda url, timeout: payload)
    out = il._from_openlibrary("0415128269", 5)
    assert out["title"] == "Rethinking Architecture"
    assert out["authors"] == ["Neil Leach"]
    assert out["publisher"] == "Routledge"
    assert out["year"] == 1997
    assert out["_source"] == "Open Library"


def test_google_parsing_and_language(monkeypatch):
    payload = {
        "items": [
            {
                "volumeInfo": {
                    "title": "Pozemné staviteľstvo",
                    "authors": ["Jozef Oláh"],
                    "publisher": "STU",
                    "publishedDate": "2009-05",
                    "language": "sk",
                }
            }
        ]
    }
    monkeypatch.setattr(il, "_get_json", lambda url, timeout: payload)
    out = il._from_google("9788022750462", 5)
    assert out["year"] == 2009
    assert out["language"] == "sk"
    assert out["_source"] == "Google Books"


def test_enricher_invalid_isbn_no_network(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not hit network for invalid ISBN")

    monkeypatch.setattr(il, "_get_json", boom)
    enr = il.IsbnEnricher(cache_dir=tmp_path, delay=0)
    assert enr.lookup("8005000") == {}  # too short -> invalid -> skipped


def test_enricher_cache_hit(tmp_path, monkeypatch):
    (tmp_path / "0415128269.json").write_text(json.dumps({"title": "Cached", "_source": "x"}))

    def boom(*a, **k):
        raise AssertionError("cache hit must not hit network")

    monkeypatch.setattr(il, "_get_json", boom)
    enr = il.IsbnEnricher(cache_dir=tmp_path, delay=0)
    assert enr.lookup("0-415-12826-9")["title"] == "Cached"


def test_enricher_negative_cache(tmp_path, monkeypatch):
    calls = {"n": 0}

    def miss(url, timeout):
        calls["n"] += 1
        return None  # 4xx-style genuine miss

    monkeypatch.setattr(il, "_get_json", miss)
    enr = il.IsbnEnricher(cache_dir=tmp_path, providers=("openlibrary",), delay=0)
    assert enr.lookup("9780500203958") == {}
    n_after_first = calls["n"]
    assert n_after_first > 0
    assert enr.lookup("9780500203958") == {}  # served from negative cache
    assert calls["n"] == n_after_first


def test_enricher_transient_not_cached(tmp_path, monkeypatch):
    def throttled(url, timeout):
        raise il.LookupTransientError("HTTP 429")

    monkeypatch.setattr(il, "_get_json", throttled)
    enr = il.IsbnEnricher(cache_dir=tmp_path, providers=("openlibrary",), delay=0)
    assert enr.lookup("9780500203958") == {}
    assert enr.transient == 1
    # a transient error must NOT be cached, so a later run can retry it
    assert not (tmp_path / "9780500203958.json").exists()


_STU_RSS = """<?xml version="1.0"?><rss><channel>
<title>STU - IPAC</title>
<description>Nájdených záznamov: 1.</description>
<item>
<title>Experimentálne metódy v mechanike</title>
<dc:creator>Starek, Ladislav</dc:creator>
<pubDate>Thu, 20 Sep 2007 01:00:00 GMT</pubDate>
<dc:identifier>urn:ISBN:978-80-227-2656-6</dc:identifier>
<description><![CDATA[ <div>Experimentálne metódy
<a href="x">Starek Ladislav</a> ; J250&nbsp;
<a href="y">Chmelko Vladim&#237;r</a> ; J220
<br>1. vyd.
<br>Bratislava : STU v Bratislave SjF, 2007
<br>ISBN 978-80-227-2656-6
<a href="z">mechanika</a></div> ]]></description>
</item></channel></rss>"""

_STU_EMPTY = '<rss><channel><description>Nájdených záznamov: 0.</description></channel></rss>'


def test_stu_opac_parsing(monkeypatch):
    monkeypatch.setattr(il, "_get_text", lambda url, timeout: _STU_RSS)
    out = il._from_stu_opac("9788022726566", 5)
    assert out["title"] == "Experimentálne metódy v mechanike"
    assert out["authors"] == ["Starek Ladislav", "Chmelko Vladimír"]  # from "; J###", not subjects
    assert out["year"] == 2007
    assert out["place"] == "Bratislava"
    assert out["publisher"] == "STU v Bratislave SjF"
    assert out["edition"] == "1. vyd."
    assert out["_source"] == "STU IPAC"


def test_stu_opac_no_record(monkeypatch):
    monkeypatch.setattr(il, "_get_text", lambda url, timeout: _STU_EMPTY)
    assert il._from_stu_opac("9999999999999", 5) == {}


# pubDate is a 2-digit year ('98) -> _year() is None; the imprint year must win.
_STU_OLD = """<rss><channel><description>Nájdených záznamov: 1.</description>
<item>
<title>Inžinierske siete</title>
<dc:creator>Uhliarik, Anton</dc:creator>
<pubDate>Thu, 02 Jul 98 01:00:00 GMT</pubDate>
<description><![CDATA[ <div>
<a href="x">Uhliarik Anton</a> ; J250
<br>        Bratislava : Alfa, 1992
        . - 291 s
<br>ISBN 80-05-00025-1</div> ]]></description>
</item></channel></rss>"""


def test_stu_opac_year_falls_back_to_imprint(monkeypatch):
    monkeypatch.setattr(il, "_get_text", lambda url, timeout: _STU_OLD)
    out = il._from_stu_opac("8005000251", 5)
    assert out["year"] == 1992  # not None from the 2-digit pubDate
    assert out["publisher"] == "Alfa"
    assert out["place"] == "Bratislava"


class _FakeEnricher:
    def lookup(self, isbn):
        return {
            "title": "Rethinking Architecture",
            "authors": ["Neil Leach"],
            "year": 1997,
            "publisher": "Routledge",
            "_source": "Open Library",
        }


def test_build_rows_with_enricher_fills_and_marks_source():
    rows = build_draft_rows(
        [DraftBook("fad", "CVI_OPACID_FA_0415128269", "FAD")], enricher=_FakeEnricher()
    )
    row = rows[0]
    assert row[HEADERS["title"]] == "Rethinking Architecture"
    assert row[HEADERS["authors"]] == "Neil Leach"
    assert row[HEADERS["year"]] == 1997
    assert row[HEADERS["source_meta"]] == "Open Library"


def test_build_rows_slug_dir_not_enriched():
    rows = build_draft_rows(
        [DraftBook("svf", "CVI_OPACID_SVF_Betonove_konstrukcie", "SVF")], enricher=_FakeEnricher()
    )
    # no ISBN in the dir -> enricher not called -> source stays empty, title is the guess
    assert rows[0][HEADERS["source_meta"]] == ""
    assert rows[0][HEADERS["title"]] == "Betonove Konstrukcie"
