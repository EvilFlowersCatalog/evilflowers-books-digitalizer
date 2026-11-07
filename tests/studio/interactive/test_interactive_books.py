"""Projects, imports, book listing/detail, IA metadata edits, artifact downloads, IA export."""

from __future__ import annotations

import io
import zipfile

from evilflowers_books_digitalizer.studio.events import EventType

from .conftest import seed_book


def test_projects_count_books_per_collection(client, services, tmp_path, seeded):
    seed_book(services, tmp_path, external_id="B2", status="approved")
    r = client.get("/api/v1/projects")
    assert r.status_code == 200
    stu = next(p for p in r.json() if p["slug"] == "stu")
    assert stu["counts"] == {"review": 1, "approved": 1} and stu["auto_publish"] == "review"
    assert stu["collections"] == {"fei": "FEI STU"}
    assert stu["collection_counts"] == {"fei": {"review": 1, "approved": 1}}
    quiet = next(p for p in r.json() if p["slug"] == "quiet")
    assert quiet["counts"] == {} and quiet["collection_counts"] == {"fei": {}}


def test_project_config_is_read_only_and_secret_free(client):
    cfg = client.get("/api/v1/projects/stu/config").json()
    assert cfg["slug"] == "stu" and cfg["layout_detectors"] == ["heuristic"]
    assert cfg["publish"]["api_key_env"] == "EVILFLOWERS_API_KEY"     # the name, never a value
    assert client.get("/api/v1/projects/nope/config").status_code == 404


def test_import_enqueues_discover(client, queue, bus):
    r = client.post("/api/v1/projects/stu/imports", json={"collection": "fei"})
    assert r.status_code == 202 and r.json()["task_id"]
    assert queue.calls == [("studio.discover", ("stu", "fei"), {})]
    assert bus.published[-1].type == EventType.IMPORT_STARTED
    assert client.post("/api/v1/projects/stu/imports").status_code == 202      # no body = all
    assert queue.calls[-1] == ("studio.discover", ("stu", None), {})
    assert client.post("/api/v1/projects/nope/imports", json={}).status_code == 404
    assert client.post("/api/v1/projects/stu/imports", json={"collection": "x"}).status_code == 422


def test_list_books_filters_sorts_and_pages(client, services, tmp_path):
    seed_book(services, tmp_path, external_id="A", title="Algebra", status="review",
              quality={"passed": False, "flags": ["low OCR"], "score": 0.4})
    seed_book(services, tmp_path, external_id="B", title="Biológia", status="approved",
              quality={"passed": True, "flags": [], "score": 0.9})
    seed_book(services, tmp_path, external_id="C", title="Chémia", status="review", project="quiet")
    r = client.get("/api/v1/books", params={"project": "stu"})
    assert r.json()["total"] == 2
    flagged = client.get("/api/v1/books", params={"flagged": True}).json()
    assert [b["external_id"] for b in flagged["items"]] == ["A"]
    assert flagged["items"][0]["flags"] == ["low OCR"] and flagged["items"][0]["flagged"]
    assert flagged["items"][0]["slug"] == "stu_fei_A" and flagged["items"][0]["progress"] == 0.0
    not_flagged = client.get("/api/v1/books", params={"flagged": False}).json()
    assert {b["external_id"] for b in not_flagged["items"]} == {"B", "C"}
    by_score = client.get("/api/v1/books", params={"sort": "-quality_score"}).json()["items"]
    assert [b["external_id"] for b in by_score] == ["B", "A", "C"]                 # nulls last
    assert client.get("/api/v1/books", params={"q": "bio"}).json()["total"] == 1
    assert client.get("/api/v1/books", params={"status": "approved"}).json()["total"] == 1
    page = client.get("/api/v1/books", params={"limit": 1, "offset": 1, "sort": "title"}).json()
    assert page["total"] == 3 and [b["title"] for b in page["items"]] == ["Biológia"]
    assert client.get("/api/v1/books", params={"sort": "nope"}).status_code == 422
    assert client.get("/api/v1/books", params={"status": "nope"}).status_code == 422


def test_book_detail_has_paths_artifacts_and_counts(client, seeded):
    r = client.get(f"/api/v1/books/{seeded.book_id}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["book"]["item_id"] == "stu-fei-book1" and d["item_metadata"]["title"] == "Vektory"
    assert d["pages"] == {"total": 2, "access": 2, "disputed": 1}
    arts = {a["kind"]: a for a in d["artifacts"]}
    assert arts["pdf"]["exists"] and arts["pdf"]["url"].endswith(f"/books/{seeded.book_id}/files/pdf")
    assert arts["pdf"]["size"] > 0
    assert arts["epub"]["exists"] is False and arts["epub"]["path"]       # placed by the store
    assert d["paths"]["source_dir"] and d["paths"]["item_dir"]
    assert d["publication"] is None and d["runs"] == []
    assert client.get("/api/v1/books/999").status_code == 404


def test_books_carry_their_open_dispute_count(client, services, tmp_path, seeded):
    seed_book(services, tmp_path, external_id="CLEAN", pages=False)
    items = {b["external_id"]: b for b in client.get("/api/v1/books").json()["items"]}
    assert items["BOOK1"]["disputed"] == 1 and items["CLEAN"]["disputed"] == 0
    assert client.get(f"/api/v1/books/{seeded.book_id}").json()["book"]["disputed"] == 1


def test_metadata_patch_validates_ia_rules(client, seeded):
    url = f"/api/v1/books/{seeded.book_id}/metadata"
    r = client.patch(url, json={"title": "Vektory II", "creator": "Červeň, Ivan",
                                "language": ["slk", "eng"], "date": "2007"})
    assert r.status_code == 200, r.text
    assert r.json()["creator"] == ["Červeň, Ivan"] and r.json()["language"] == ["slo", "eng"]
    assert client.get(f"/api/v1/books/{seeded.book_id}").json()["book"]["title"] == "Vektory II"
    assert client.patch(url, json={"identifier": "other-id"}).status_code == 409
    assert client.patch(url, json={"date": "July 2007"}).status_code == 422
    assert client.patch(url, json={"scandate": "2026"}).status_code == 422
    assert client.patch(url, json={"page_progression": "tb"}).status_code == 422
    assert client.patch(url, json={"nonsense": 1}).status_code == 422
    assert client.patch(url, json={"ppi": "many"}).status_code == 422
    assert client.patch(url, json={"extra": {"faculty": ["fei"]}}).status_code == 422
    ok = client.patch(url, json={"extra": {"evilflowers_faculty": ["fei"]}, "ppi": 600})
    assert ok.status_code == 200 and ok.json()["ppi"] == 600
    assert client.patch("/api/v1/books/999/metadata", json={}).status_code == 404


def test_artifact_download(client, seeded):
    r = client.get(f"/api/v1/books/{seeded.book_id}/files/pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert client.get(f"/api/v1/books/{seeded.book_id}/files/epub").status_code == 404   # not made
    assert client.get(f"/api/v1/books/{seeded.book_id}/files/..%2Fetc").status_code == 404
    assert client.get("/api/v1/books/999/files/pdf").status_code == 404


def test_ia_export_zip(client, services, tmp_path, seeded):
    r = client.get(f"/api/v1/books/{seeded.book_id}/export/ia")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    for suffix in ("_meta.xml", "_scandata.xml", "_page_numbers.json", "_files.xml", ".pdf",
                   "_jp2.zip"):      # jp2 package built from the leaves' cleaned images
        assert f"stu-fei-book1/stu-fei-book1{suffix}" in names, suffix
    bare = seed_book(services, tmp_path, external_id="NOPAGES", status="discovered", pages=False)
    assert client.get(f"/api/v1/books/{bare.book_id}/export/ia").status_code == 409
    assert client.get("/api/v1/books/999/export/ia").status_code == 404
