"""Leaves, page edits (IA scandata + overrides), region edits, notes, image layers, deep zoom."""

from __future__ import annotations

import io

from PIL import Image

from evilflowers_books_digitalizer.studio.events import EventType


def test_list_pages_and_access_index(client, seeded):
    r = client.get(f"/api/v1/books/{seeded.book_id}/pages")
    assert r.status_code == 200
    pages = r.json()
    assert [p["index"] for p in pages] == [0, 1] and [p["access_index"] for p in pages] == [0, 1]
    assert pages[0]["disputed"] == 1 and pages[0]["hand_side"] == "LEFT"
    assert pages[0]["thumb_url"].endswith(f"/pages/{seeded.page_ids[0]}/image/clean?w=240")
    assert client.get("/api/v1/books/999/pages").status_code == 404


def test_page_detail(client, seeded):
    r = client.get(f"/api/v1/pages/{seeded.page_ids[0]}")
    assert r.status_code == 200
    d = r.json()
    assert d["regions"][0]["kind"] == "stamp" and d["regions"][0]["status"] == "disputed"
    assert d["frame"]["exists"] and d["frame"]["crop_box"] == [0, 0, 200, 300]
    assert (d["prev_id"], d["next_id"]) == (None, seeded.page_ids[1])
    assert d["files"]["clean"]["exists"] and d["files"]["dzi"]["exists"] is False
    assert d["ocr_text"] == "text 0" and set(d["images"]) == {"original", "spread", "clean", "pdf", "dzi"}
    assert client.get("/api/v1/pages/999").status_code == 404


def test_page_type_seeds_access_flag_and_reindexes(client, seeded, bus):
    p0 = seeded.page_ids[0]
    r = client.patch(f"/api/v1/pages/{p0}", json={"page_type": "Color Card"})
    assert r.status_code == 200, r.text
    assert r.json()["page"]["add_to_access"] is False and r.json()["page"]["access_index"] is None
    pages = client.get(f"/api/v1/books/{seeded.book_id}/pages").json()
    assert [p["access_index"] for p in pages] == [None, 0]
    assert bus.published[-1].type == EventType.PAGE_UPDATED
    # explicit flag wins over the page type default; the spec's name is accepted too
    r = client.patch(f"/api/v1/pages/{p0}", json={"page_type": "Delete", "include_in_access": True})
    assert r.json()["page"]["add_to_access"] is True and r.json()["page"]["page_type"] == "Delete"


def test_page_number_override_and_leaf_extra(client, seeded):
    url = f"/api/v1/pages/{seeded.page_ids[1]}"
    r = client.patch(url, json={"page_number": " xii ", "override": {"deskew_deg": -0.5,
                                "crop": [10, 10, 190, 290], "despeckle": "normal"},
                                "leaf_extra": {"book_start": True, "toc_title": "Úvod"}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["page"]["page_number"] == "xii"
    assert d["override"] == {"crop": [10, 10, 190, 290], "deskew_deg": -0.5, "despeckle": "normal"}
    assert d["leaf_extra"] == {"book_start": True, "toc_title": "Úvod"}
    r = client.patch(url, json={"page_number": None, "leaf_extra": {"toc_title": None}})
    assert r.json()["page"]["page_number"] is None and r.json()["leaf_extra"] == {"book_start": True}
    assert client.patch(url, json={"override": {"zoom": 2}}).status_code == 422
    assert client.patch(url, json={"override": {"despeckle": "max"}}).status_code == 422
    assert client.patch(url, json={"override": {"crop": [10, 10, 5, 5]}}).status_code == 422
    assert client.patch(url, json={"leaf_extra": {"toc_level": "1"}}).status_code == 422
    assert client.patch(url, json={"page_type": "Weird"}).status_code == 422
    assert client.patch("/api/v1/pages/999", json={}).status_code == 404


def test_draw_region_is_operator_confirmed(client, seeded, bus):
    r = client.post(f"/api/v1/pages/{seeded.page_ids[1]}/regions",
                    json={"kind": "formula", "bbox": [20, 100, 180, 140]})
    assert r.status_code == 201, r.text
    reg = r.json()
    assert (reg["source"], reg["status"], reg["action"], reg["author"]) == \
        ("operator", "confirmed", "reocr", "jana")
    assert bus.published[-1].type == EventType.REGIONS_UPDATED
    page = f"/api/v1/pages/{seeded.page_ids[1]}/regions"
    assert client.post(page, json={"kind": "text", "bbox": [0, 0, 500, 10]}).status_code == 422
    assert client.post(page, json={"kind": "text", "bbox": [5, 5, 5, 9]}).status_code == 422
    assert client.post(page, json={"kind": "blob", "bbox": [0, 0, 5, 5]}).status_code == 422
    assert client.post("/api/v1/pages/999/regions",
                       json={"kind": "text", "bbox": [0, 0, 5, 5]}).status_code == 404


def test_resolving_the_last_dispute_clears_the_page(client, seeded, bus):
    stamp = seeded.region_ids[0]
    r = client.patch(f"/api/v1/regions/{stamp}", json={"action": "remove"})
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["source"], r.json()["reason"]) == ("confirmed", "operator", None)
    page = client.get(f"/api/v1/pages/{seeded.page_ids[0]}").json()["page"]
    assert page["status"] == "analyzed" and page["disputed"] == 0
    assert bus.published[-1].data == {"page_id": seeded.page_ids[0], "index": 0, "disputed": 0}


def test_relabel_rederives_action_and_explicit_status_wins(client, seeded):
    text = seeded.region_ids[1]
    r = client.patch(f"/api/v1/regions/{text}", json={"kind": "picture", "bbox": [0, 0, 100, 100]})
    assert (r.json()["kind"], r.json()["action"], r.json()["bbox"]) == \
        ("picture", "as_image", [0, 0, 100, 100])
    r = client.patch(f"/api/v1/regions/{text}", json={"status": "disputed"})
    assert r.json()["status"] == "disputed"
    assert client.get(f"/api/v1/pages/{seeded.page_ids[1]}").json()["page"]["status"] == "disputed"
    assert client.patch(f"/api/v1/regions/{text}", json={"bbox": [0, 0, 999, 10]}).status_code == 422
    assert client.patch("/api/v1/regions/999", json={}).status_code == 404


def test_delete_region_rejects_without_deleting(client, seeded):
    stamp = seeded.region_ids[0]
    assert client.delete(f"/api/v1/regions/{stamp}").status_code == 204
    regions = client.get(f"/api/v1/pages/{seeded.page_ids[0]}").json()["regions"]
    assert [(r["id"], r["status"], r["author"]) for r in regions] == [(stamp, "rejected", "jana")]
    assert client.delete("/api/v1/regions/999").status_code == 404


def test_notes_attach_to_the_most_specific_target(client, seeded):
    r = client.post("/api/v1/notes", json={"region_id": seeded.region_ids[0],
                                           "text": "library seal, not content"})
    assert r.status_code == 201, r.text
    n = r.json()
    assert (n["book_id"], n["page_id"], n["author"]) == (seeded.book_id, seeded.page_ids[0], "jana")
    assert client.get(f"/api/v1/pages/{seeded.page_ids[0]}").json()["notes"][0]["id"] == n["id"]
    r = client.post("/api/v1/notes", json={"book_id": seeded.book_id, "text": "check the cover"})
    assert r.status_code == 201
    assert client.get(f"/api/v1/books/{seeded.book_id}").json()["notes"][0]["text"] == "check the cover"
    assert client.post("/api/v1/notes", json={"text": "orphan"}).status_code == 400
    assert client.post("/api/v1/notes", json={"book_id": 999, "page_id": seeded.page_ids[0],
                                              "text": "x"}).status_code == 400
    assert client.post("/api/v1/notes", json={"page_id": 999, "text": "x"}).status_code == 404
    assert client.post("/api/v1/notes", json={"book_id": seeded.book_id, "text": ""}).status_code == 422


def _size(response) -> tuple[int, int]:
    assert response.status_code == 200, response.text
    return Image.open(io.BytesIO(response.content)).size


def test_image_layers(client, seeded):
    p0 = seeded.page_ids[0]
    assert _size(client.get(f"/api/v1/pages/{p0}/image/original", params={"w": 100})) == (100, 150)
    assert _size(client.get(f"/api/v1/pages/{p0}/image/spread", params={"w": 200})) == (200, 150)
    assert _size(client.get(f"/api/v1/pages/{p0}/image/clean", params={"w": 50})) == (50, 75)
    pdf = client.get(f"/api/v1/pages/{seeded.page_ids[1]}/image/pdf", params={"w": 100})
    assert _size(pdf) == (100, 150) and pdf.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/v1/pages/{p0}/image/bogus").status_code == 422
    assert client.get(f"/api/v1/pages/{p0}/image/clean", params={"w": 5}).status_code == 422
    assert client.get("/api/v1/pages/999/image/clean").status_code == 404
    client.patch(f"/api/v1/pages/{p0}", json={"page_type": "Delete"})
    assert client.get(f"/api/v1/pages/{p0}/image/pdf").status_code == 404     # not in access


def test_deep_zoom_descriptor_and_tiles(client, seeded):
    p0 = seeded.page_ids[0]
    r = client.get(f"/api/v1/pages/{p0}/dzi")
    assert r.status_code == 200 and '<Size Width="200" Height="300"/>' in r.text
    assert 'Width="400"' in client.get(f"/api/v1/pages/{p0}/dzi", params={"layer": "spread"}).text
    assert _size(client.get(f"/api/v1/pages/{p0}/dzi_files/9/0_0.jpg")) == (200, 255)  # 2**9 >= 300
    assert _size(client.get(f"/api/v1/pages/{p0}/dzi_files/9/0_1.jpg")) == (200, 47)
    assert _size(client.get(f"/api/v1/pages/{p0}/dzi_files/8/0_0.png",
                            params={"layer": "original"})) == (100, 150)
    assert client.get(f"/api/v1/pages/{p0}/dzi_files/9/5_5.jpg").status_code == 404
    assert client.get(f"/api/v1/pages/{p0}/dzi_files/9/a_b.jpg").status_code == 404
    assert client.get(f"/api/v1/pages/{p0}/dzi_files/12/0_0.jpg").status_code == 404
    assert client.get("/api/v1/pages/999/dzi").status_code == 404
    # the SPA's URL: with an extension OpenSeadragon keeps ?v= for the tile requests
    assert client.get(f"/api/v1/pages/{p0}/dzi.xml", params={"v": 2}).text == r.text
    assert client.get(f"/api/v1/pages/{p0}/dzi_files/9/0_0.jpg", params={"v": 2}).status_code == 200


def test_pre_rendered_dzi_is_served_as_is(client, services, seeded, tmp_path):
    from evilflowers_books_digitalizer.db.models import Page

    dzi = tmp_path / "studio" / "0000.dzi"
    (dzi.parent / "0000_files" / "0").mkdir(parents=True)
    dzi.write_text("<Image/>")
    Image.new("L", (1, 1)).save(dzi.parent / "0000_files" / "0" / "0_0.jpg")
    s = services.sessions()
    s.get(Page, seeded.page_ids[0]).dzi_path = str(dzi)
    s.commit()
    s.close()
    assert client.get(f"/api/v1/pages/{seeded.page_ids[0]}/dzi").text == "<Image/>"
    assert client.get(f"/api/v1/pages/{seeded.page_ids[0]}/dzi_files/0/0_0.jpg").status_code == 200
    assert client.get(f"/api/v1/pages/{seeded.page_ids[0]}/dzi_files/1/0_0.jpg").status_code == 404
