"""Labelling mode (#7): sampling from a fake catalog, serving without anchoring, export, kappa.

The catalog is the real ``CatalogLabelSource`` over an ``httpx.MockTransport`` that answers
like EvilFlowersCatalog develop (709b712), so adapter, use cases and API are tested together.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from evilflowers_books_digitalizer.studio.adapters.catalog.labelling import CatalogLabelSource
from evilflowers_books_digitalizer.studio.adapters.ia import IaItemExporter
from evilflowers_books_digitalizer.studio.api.app import create_app
from evilflowers_books_digitalizer.studio.application import labelling as app
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.config import LabellingConfig
from evilflowers_books_digitalizer.studio.labelling import cohens_kappa, region_key, stratified, udc_top

E1, E2, CAT = "e1000000-0000-0000-0000-000000000001", "e2000000-0000-0000-0000-000000000002", "c1"
WEBP = b"RIFF\x00\x00\x00\x00WEBPfake"

#: what must never reach a labeller (the model's answer, model-written text)
ANCHORS = ("band", "decision", "decider_model", "relation", "relation_p", "description", "category",
           "hidden", "stratum", "categories")


def _region(rid: str, kind: str, page: int, seq: int, band: str | None, **extra: Any) -> dict[str, Any]:
    return {"id": rid, "run_id": "run-1", "kind": kind, "page": page, "seq": seq,
            "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.4}, "caption": "c",
            "printed_caption": f"Obr. {seq}", "description": "a photo of a stamp (model-written)",
            "category": "photo", "markdown": None, "latex": None, "provenance": {},
            "band": band, "p": 0.61 if band else None, "decision": "stamp" if band else None,
            "decider_model": "strands@1" if band else None, **extra}


REGIONS = {
    (E1, "figure"): [_region("r1", "figure", 12, 1, "review"), _region("r2", "figure", 12, 2, "keep"),
                     _region("r3", "figure", 14, 1, None)],
    (E2, "figure"): [_region("r4", "figure", 3, 1, "hide"), _region("r5", "figure", 5, 1, None)],
    (E1, "table"): [_region("t1", "table", 20, 1, None, markdown="| a | b |\n|---|---|\n| 1 | 2 |")],
    (E2, "table"): [],
}
ENTRIES = [
    {"id": E1, "catalog_id": CAT, "title": "Vektory", "summary": "Lineárna algebra.",
     "language": {"alpha2": "sk"}, "categories": [{"id": "cat-51", "term": "51"}]},
    {"id": E2, "catalog_id": CAT, "title": "Vectors", "summary": "Linear algebra.",
     "language": {"alpha2": "en"}, "categories": []},
]


class FakeCatalog:
    """Answers GETs like the catalog; records every request (method, path, auth header)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET", "the labelling adapter must only read"
        path = request.url.path.removeprefix("/api/v1/")
        q = request.url.params
        parts = path.split("/")
        if path == "entries":
            return httpx.Response(200, json={"items": ENTRIES, "metadata": {"page": 1, "limit": 50,
                                                                            "pages": 1, "total": 2}})
        if parts[0] == "entries" and parts[2:4] == ["extraction", "regions"] and len(parts) == 4:
            return httpx.Response(200, json={"response": {
                "run_id": "run-1", "items": REGIONS[(parts[1], q["kind"])]}})
        if parts[0] == "entries" and parts[-1] == "image":
            return httpx.Response(200, content=WEBP, headers={"content-type": "image/webp"})
        if parts[0] == "entries" and parts[2:] == ["extraction", "paragraphs"]:
            page = int(q["pdf_page"])
            heading = {1: "Úvod", 2: "Úvod", 3: "Báza", 12: "Obrázky"}.get(page, "Iné")
            return httpx.Response(200, json={"response": {"run_id": "run-1", "items": [
                {"page": page, "section": heading, "text": f"Text strany {page} pod {heading}."}]}})
        if parts[0] == "entries" and parts[2:] == ["chapters"]:
            if parts[1] == E1:
                return httpx.Response(200, json={"entry_id": E1, "sections": [
                    {"key": "s1", "seq": 1, "title": "Úvod", "first_page": 1, "last_page": 2, "topical": True},
                    {"key": "s2", "seq": 2, "title": "Báza", "first_page": 3, "last_page": 4, "topical": True}],
                    "links": [{"source": "s1", "target": "s2", "links": 3}]})
            return httpx.Response(200, json={"entry_id": E2, "sections": [], "links": []})
        if parts[0] == "sections" and parts[2] == "related":
            hits = [{"key": "x9", "entry_id": E2, "title": "Basis", "first_page": 7, "score": 0.81,
                     "via": "similar", "relation": "prerequisite", "relation_p": 0.7}] if parts[1] == "s1" else []
            return httpx.Response(200, json=hits)
        if parts[0] == "catalogs" and parts[2] == "entries":
            return httpx.Response(200, json={"response": {
                "id": parts[3], "table_of_contents": ["1 Úvod", {"title": "2 Báza", "level": 1}]}})
        if path == "categories":
            return httpx.Response(200, json={"items": [
                {"id": "cat-004", "term": "004.8", "label": {"sk": "Umelá inteligencia"}},
                {"id": "cat-51", "term": "51", "label": {"sk": "Matematika", "en": "Mathematics"}}]})
        return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture
def catalog() -> FakeCatalog:
    return FakeCatalog()


@pytest.fixture
def lab_services(services: Services, catalog: FakeCatalog) -> Services:
    cfg = LabellingConfig(base_url="https://catalog.test/api/v1", seed=1)
    http = httpx.Client(base_url=cfg.base_url + "/", transport=httpx.MockTransport(catalog),
                        headers={"Authorization": "Bearer test-key"})
    source = CatalogLabelSource(cfg, client=http)
    return dataclasses.replace(services, config=dataclasses.replace(services.config, labelling=cfg),
                               label_source=lambda: source)


@pytest.fixture
def lab_client(lab_services: Services, tmp_path: Path) -> TestClient:
    created = create_app(services=lab_services, ia_exporter=IaItemExporter(),
                         static_dir=tmp_path / "dist", ping_seconds=0.2)
    return TestClient(created, headers={"X-Operator": "jana"})


def _as(client: TestClient, name: str) -> dict[str, str]:
    return {"X-Operator": name}


# ---------------------------------------------------------------- pure rules
def test_region_key_matches_the_issue_example():
    assert region_key("figure", 12, 1) == "p012-fig01"
    assert region_key("table", 3, 2) == "p003-tab02"


def test_stratified_round_robin_keeps_small_strata():
    picked = stratified({"en/none": ["a", "b", "c", "d"], "sk/review": ["x"]}, 3)
    assert "x" in picked and len(picked) == 3


def test_udc_top_level_is_the_first_digit():
    assert udc_top("004.8") == "0" and udc_top("51") == "5" and udc_top(None) == "?"


def test_cohens_kappa():
    assert cohens_kappa([("a", "a"), ("b", "b"), ("a", "a"), ("b", "b")]) == pytest.approx(1.0)
    # observed 0.5, expected 0.5 -> 0
    assert cohens_kappa([("a", "a"), ("a", "b"), ("b", "a"), ("b", "b")]) == pytest.approx(0.0)
    assert cohens_kappa([("a", "a")]) is None
    assert cohens_kappa([("a", "a"), ("a", "a")]) is None   # chance agreement 1: undefined


# ---------------------------------------------------------------- sampling
def test_sample_is_stratified_idempotent_and_read_only(lab_services: Services, catalog: FakeCatalog):
    out = app.sample(lab_services, "figure_kind", 4)
    assert out.added == 4 and out.candidates == 5
    # five language x band strata of one item each: four picks come from four different ones
    assert len(out.strata) == 4 and set(out.strata.values()) == {1}
    assert set(out.strata) <= {"sk/review", "sk/keep", "sk/none", "en/hide", "en/none"}
    again = app.sample(lab_services, "figure_kind", 10)
    assert again.added == 1 and again.already_sampled == 4
    assert all(r.method == "GET" for r in catalog.requests)
    assert all(r.headers["Authorization"] == "Bearer test-key" for r in catalog.requests)


def test_without_bands_the_sample_stratifies_on_p(lab_services: Services, monkeypatch: pytest.MonkeyPatch):
    """Bands stay empty until knowledge#65 sets thresholds: the unsure cases come from ``p``."""
    regions = {k: [dict(r, band=None) for r in v] for k, v in REGIONS.items()}
    regions[(E2, "figure")][0]["p"] = 0.42
    monkeypatch.setitem(REGIONS, (E1, "figure"), regions[(E1, "figure")])
    monkeypatch.setitem(REGIONS, (E2, "figure"), regions[(E2, "figure")])
    out = app.sample(lab_services, "figure_kind", 5)
    assert "en/p<0.5" in out.strata and "sk/p<0.8" in out.strata


def test_an_entry_the_key_cannot_manage_is_skipped(lab_services: Services, catalog: FakeCatalog):
    def refuse_e1(request: httpx.Request) -> httpx.Response:
        if f"entries/{E1}/extraction" in request.url.path:
            return httpx.Response(403, json={"detail": "forbidden"})
        return catalog(request)

    http =httpx.Client(base_url="https://catalog.test/api/v1/", transport=httpx.MockTransport(refuse_e1))
    source = CatalogLabelSource(lab_services.config.labelling, client=http)
    svc = dataclasses.replace(lab_services, label_source=lambda: source)
    out = app.sample(svc, "figure_kind", 5)
    assert out.added == 2 and source.refused == 1      # E2's two figures; E1 refused, not fatal


def test_a_skip_leaves_the_item_open_for_others(lab_services: Services, lab_client: TestClient):
    app.sample(lab_services, "is_data_table", 5)     # one table item
    item = lab_client.get("/api/v1/labelling/tasks/is_data_table/next").json()
    lab_client.post(f"/api/v1/labelling/items/{item['id']}/labels",
                    json={"labels": [{"task": "is_data_table", "value": "skip"}]})
    assert lab_client.get("/api/v1/labelling/tasks/is_data_table/next").json() is None
    other = lab_client.get("/api/v1/labelling/tasks/is_data_table/next", headers={"X-Operator": "petra"})
    assert other.json()["id"] == item["id"]


def test_sample_without_config_is_unavailable(services: Services, client: TestClient):
    from evilflowers_books_digitalizer.studio.application.errors import Unavailable

    with pytest.raises(Unavailable):
        app.sample(services, "figure_kind", 5)


def test_title_page_cannot_be_sampled_over_the_api(client: TestClient):
    r = client.post("/api/v1/labelling/tasks/title_page/samples")
    assert r.status_code == 400 and "confirmed metadata forms" in r.text
    # scan_mark samples the studio's own groups: no [labelling] catalog needed
    assert client.post("/api/v1/labelling/tasks/scan_mark/samples").status_code == 202


# ---------------------------------------------------------------- serving: no anchoring
def _walk_keys(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _walk_keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _walk_keys(v)}
    return set()


@pytest.mark.parametrize("task", ["figure_kind", "is_data_table", "section_relation", "subject"])
def test_items_never_carry_the_models_answer(lab_services: Services, lab_client: TestClient, task: str):
    app.sample(lab_services, task, 10)
    item = lab_client.get(f"/api/v1/labelling/tasks/{task}/next").json()
    assert item is not None
    leaked = _walk_keys(item) & set(ANCHORS)
    assert not leaked, f"{task} item leaks {leaked}"
    for word in ("review", "prerequisite", "model-written", "strands"):
        assert word not in json.dumps(item, ensure_ascii=False)


def test_figure_item_payload_and_image_proxy(lab_services: Services, lab_client: TestClient):
    app.sample(lab_services, "figure_kind", 5)
    item = lab_client.get("/api/v1/labelling/tasks/figure_kind/next").json()
    assert item["ref"]["region"].startswith("p0") and item["ref"]["run_id"] == "run-1"
    assert item["image_url"] == f"/api/v1/labelling/items/{item['id']}/image"
    img = lab_client.get(item["image_url"])
    assert img.status_code == 200 and img.content == WEBP
    assert img.headers["content-type"] == "image/webp"


def test_section_pair_has_text_on_both_sides_and_a_is_the_sampled_section(lab_services: Services,
                                                                          lab_client: TestClient):
    out = app.sample(lab_services, "section_relation", 5)
    # s1-s2 (intra-book) is kept; s1-x9 (cross-book) is dropped: no paragraph of E2 sits
    # under the heading "Basis", and a labeller cannot judge a heading against a text
    assert out.added == 1 and out.candidates == 2
    seen = []
    while (item := lab_client.get("/api/v1/labelling/tasks/section_relation/next").json()):
        seen.append(item)
        lab_client.post(f"/api/v1/labelling/items/{item['id']}/labels",
                        json={"labels": [{"task": "section_relation", "value": "loose"}]})
    intra = next(i for i in seen if i["ref"]["section_b"] == "s2")
    assert intra["ref"]["section_a"] == "s1"
    assert "Text strany 1 pod Úvod" in intra["payload"]["a"]["text"]
    assert "Báza" in intra["payload"]["b"]["text"]
    assert intra["language"] == "sk"


# ---------------------------------------------------------------- answers
def test_labels_are_validated_replaced_and_require_a_name(lab_services: Services, lab_client: TestClient):
    app.sample(lab_services, "figure_kind", 5)
    item = lab_client.get("/api/v1/labelling/tasks/figure_kind/next").json()
    url = f"/api/v1/labelling/items/{item['id']}/labels"
    bad = lab_client.post(url, json={"labels": [{"task": "figure_kind", "value": "cat"}]})
    assert bad.status_code == 400 and "chart" in bad.json()["detail"]
    wrong_kind = lab_client.post(url, json={"labels": [{"task": "is_data_table", "value": "yes"}]})
    assert wrong_kind.status_code == 400
    anon = TestClient(lab_client.app).post(url, json={"labels": [{"task": "figure_kind", "value": "map"}]})
    assert anon.status_code == 400
    assert lab_client.post(url, json={"labels": [{"task": "figure_kind", "value": "map"}]}).json()["mine"] == \
        {"figure_kind": "map"}
    again = lab_client.post(url, json={"labels": [{"task": "figure_kind", "value": "stamp"}]}).json()
    assert again["mine"] == {"figure_kind": "stamp"}
    lines = lab_client.get("/api/v1/labelling/tasks/figure_kind/export").text.splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["label"] == "stamp"


def test_table_screen_answers_both_tasks(lab_services: Services, lab_client: TestClient):
    app.sample(lab_services, "is_data_table", 5)
    item = lab_client.get("/api/v1/labelling/tasks/is_data_table/next").json()
    assert item["payload"]["markdown"].startswith("| a |")
    out = lab_client.post(f"/api/v1/labelling/items/{item['id']}/labels", json={"labels": [
        {"task": "is_data_table", "value": "yes"}, {"task": "table_quality", "value": "minor_errors"}]})
    assert out.json()["mine"] == {"is_data_table": "yes", "table_quality": "minor_errors"}
    assert lab_client.get("/api/v1/labelling/tasks/table_quality/next").json() is None


def test_subject_labels_are_the_catalogs_category_ids(lab_services: Services, lab_client: TestClient):
    app.sample(lab_services, "subject", 5)
    item = lab_client.get("/api/v1/labelling/tasks/subject/next").json()
    assert [o["term"] for o in item["payload"]["options"]] == ["004.8", "51"]
    assert item["payload"]["table_of_contents"] == ["1 Úvod", "  2 Báza"]
    url = f"/api/v1/labelling/items/{item['id']}/labels"
    assert lab_client.post(url, json={"labels": [{"task": "subject", "value": "51"}]}).status_code == 400
    lab_client.post(url, json={"labels": [{"task": "subject", "value": "cat-51"}]})
    line = json.loads(lab_client.get("/api/v1/labelling/tasks/subject/export").text)
    assert line["label"] == "cat-51" and line["label_term"] == "51"


# ---------------------------------------------------------------- export, second labeller, kappa
def test_export_line_shape_skip_and_second_labeller(lab_services: Services, lab_client: TestClient):
    app.sample(lab_services, "figure_kind", 5)

    def answer(who: str, value: str) -> dict[str, Any] | None:
        item = lab_client.get("/api/v1/labelling/tasks/figure_kind/next", headers=_as(lab_client, who)).json()
        if item is not None:
            lab_client.post(f"/api/v1/labelling/items/{item['id']}/labels", headers=_as(lab_client, who),
                            json={"labels": [{"task": "figure_kind", "value": value}]})
        return item

    first = [answer("jana", v) for v in ("chart", "photo", "skip", "map", "stamp")]
    assert all(first) and answer("jana", "chart") is None        # jana is done
    # petra is offered an item jana answered (non-skip): the 10 % second-labeller share
    second = answer("petra", "chart")
    assert second is not None and second["id"] == first[0]["id"]

    lines = [json.loads(x) for x in lab_client.get("/api/v1/labelling/tasks/figure_kind/export").text.splitlines()]
    assert len(lines) == 5 and all(line["label"] != "skip" for line in lines)
    line = lines[0]
    assert set(line) == {"task", "item", "language", "label", "labeller", "labelled_at", "via",
                         "labeller_kind", "model"}
    assert (line["via"], line["labeller_kind"], line["model"]) == ("spa", "human", None)
    assert set(line["item"]) >= {"entry_id", "run_id", "region"} and line["labelled_at"].endswith("Z")

    summary = lab_client.get("/api/v1/labelling/tasks/figure_kind/summary").json()
    assert summary["labels"] == 5 and summary["skipped"] == 1 and summary["double"] == 1
    assert summary["agreement"] == 1.0 and summary["done"] is False
    assert summary["labellers"] == {"jana": 4, "petra": 1}

    tasks = {t["name"]: t for t in lab_client.get("/api/v1/labelling/tasks").json()}
    assert tasks["figure_kind"]["labelled"] == 5 and tasks["figure_kind"]["mine"] == 5
    assert tasks["table_quality"]["companion_of"] == "is_data_table"


def test_cli_export_writes_jsonl_and_summary(lab_services: Services, lab_client: TestClient,
                                              tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from evilflowers_books_digitalizer import cli

    app.sample(lab_services, "figure_kind", 2)
    item = lab_client.get("/api/v1/labelling/tasks/figure_kind/next").json()
    lab_client.post(f"/api/v1/labelling/items/{item['id']}/labels",
                    json={"labels": [{"task": "figure_kind", "value": "diagram"}]})
    monkeypatch.setattr(cli, "_studio_services", lambda args: lab_services)
    out = tmp_path / "labels" / "figure_kind.jsonl"
    args = cli.build_parser().parse_args(["studio", "export-labels", "--task", "figure_kind",
                                          "--out", str(out)])
    assert args.func(args) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["label"] == "diagram"
    assert json.loads((tmp_path / "labels" / "figure_kind.jsonl.summary.json").read_text())["labels"] == 1


def test_unknown_task_is_404(lab_client: TestClient):
    assert lab_client.get("/api/v1/labelling/tasks/nope/next").status_code == 404


# ---------------------------------------------------------------- the three interfaces
def test_spa_can_start_a_sample(lab_services: Services, lab_client: TestClient):
    resp = lab_client.post("/api/v1/labelling/tasks/figure_kind/samples", params={"n": 3})
    assert resp.status_code == 202 and resp.json()["n"] == 3
    # TestClient runs background tasks before returning
    tasks = {t["name"]: t for t in lab_client.get("/api/v1/labelling/tasks").json()}
    assert tasks["figure_kind"]["items"] == 3
    assert lab_client.post("/api/v1/labelling/tasks/nope/samples").status_code == 404


def test_cli_label_loop_records_via_cli(lab_services: Services, tmp_path: Path):
    from evilflowers_books_digitalizer.studio.labelling_cli import label_loop, status_table

    app.sample(lab_services, "is_data_table", 5)
    app.sample(lab_services, "subject", 5)
    said: list[str] = []
    answers = iter(["7", "1", "3"])          # not a choice, then yes, then table_quality "broken"
    n = label_loop(lab_services, "is_data_table", "jana", ask=lambda _p: next(answers),
                   say=said.append, crops=tmp_path / "crops")
    assert n == 1
    assert any("| a | b |" in s for s in said) and any("not a choice" in s for s in said)
    assert (tmp_path / "crops").glob("*.webp")
    keys = iter(["2", "1", "q"])             # top level 5 (second group), its first category; quit
    assert label_loop(lab_services, "subject", "jana", ask=lambda _p: next(keys), say=said.append) == 1
    lines = [*app.export_lines(lab_services, "is_data_table"), *app.export_lines(lab_services, "table_quality"),
             *app.export_lines(lab_services, "subject")]
    assert [(x["task"], x["label"], x["via"]) for x in lines] == [
        ("is_data_table", "yes", "cli"), ("table_quality", "broken", "cli"), ("subject", "cat-51", "cli")]
    assert "is_data_table" in status_table(lab_services, "jana")


def test_cli_label_quits_and_refuses_the_companion_task(lab_services: Services):
    from evilflowers_books_digitalizer.studio.application.errors import BadRequest
    from evilflowers_books_digitalizer.studio.labelling_cli import label_loop

    app.sample(lab_services, "figure_kind", 2)
    assert label_loop(lab_services, "figure_kind", "jana", ask=lambda _p: "q", say=lambda _s: None) == 0
    with pytest.raises(BadRequest):
        label_loop(lab_services, "table_quality", "jana", ask=lambda _p: "q", say=lambda _s: None)


def test_mcp_tools_cover_the_workflow(lab_services: Services):
    import anyio
    from mcp.types import ImageContent, TextContent

    from evilflowers_books_digitalizer.studio.api.mcp import build_mcp

    mcp = build_mcp(lab_services)

    async def flow() -> None:
        names = {t.name for t in await mcp.list_tools()}
        assert names == {"list_label_tasks", "next_label_item", "submit_label", "sample_label_items",
                         "label_summary", "export_labels"}
        _, sampled = await mcp.call_tool("sample_label_items", {"task": "figure_kind", "n": 2})
        assert sampled["added"] == 2
        content = await mcp.call_tool("next_label_item", {"task": "figure_kind", "labeller": "jana"})
        texts = [c for c in content if isinstance(c, TextContent)]
        assert any(isinstance(c, ImageContent) for c in content), "the crop goes to the client"
        item = json.loads(texts[0].text)
        assert not (_walk_keys(item) & set(ANCHORS)), "no model answer over MCP either"
        await mcp.call_tool("submit_label", {"item_id": item["item"]["id"], "task": "figure_kind",
                                             "value": "chart", "labeller": "jana"})
        content, _ = await mcp.call_tool("export_labels", {"task": "figure_kind"})
        line = json.loads(content[0].text)   # type: ignore[union-attr]
        assert (line["label"], line["via"], line["labeller"]) == ("chart", "mcp", "jana")
        bad = await mcp.call_tool("submit_label", {"item_id": item["item"]["id"], "task": "figure_kind",
                                                   "value": "cat", "labeller": "jana"})
        assert bad   # a ToolError is raised before this line

    with pytest.raises(Exception, match="not a figure_kind label"):
        anyio.run(flow)


def test_mcp_over_http_on_the_portal(lab_services: Services, tmp_path: Path):
    created = create_app(services=lab_services, ia_exporter=IaItemExporter(),
                         static_dir=tmp_path / "dist", ping_seconds=0.2)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(created) as http:        # the lifespan runs the MCP session manager
        resp = http.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}}})
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"]["serverInfo"]["name"] == "evilflowers-studio"
        tools = http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert "next_label_item" in tools.text
        assert "claim_label_items" not in tools.text      # agent tools are off by default


# ---------------------------------------------------------------- agent labellers (#16)
MODEL = "claude-opus-5-5"


@pytest.fixture
def agent_services(lab_services: Services) -> Services:
    cfg = dataclasses.replace(lab_services.config.labelling, agent_labellers=True)
    return dataclasses.replace(lab_services, config=dataclasses.replace(lab_services.config, labelling=cfg))


def test_config_parses_the_agent_switch():
    from evilflowers_books_digitalizer.studio.config import _labelling

    cfg = _labelling({"agent_labellers": True, "agent_lease_minutes": 5})
    assert cfg.agent_labellers and cfg.agent_lease_minutes == 5
    assert not _labelling({}).agent_labellers


def test_parallel_claims_never_share_an_item(agent_services: Services):
    app.sample(agent_services, "figure_kind", 5)
    first = app.claim(agent_services, "figure_kind", 2, "claude", MODEL)
    second = app.claim(agent_services, "figure_kind", 2, "claude", MODEL)   # same name, in parallel
    third = app.claim(agent_services, "figure_kind", 10, "other-agent", MODEL)
    ids = [i.id for c in (first, second, third) for i in c.items]
    assert len(ids) == 5 and len(set(ids)) == 5
    empty = app.claim(agent_services, "figure_kind", 2, "claude", MODEL)
    assert empty.items == [] and empty.note and empty.lease_expires_at is None
    # the guide travels with every claim: definitions of every label, the skip rule, Slovak
    assert set(first.guide["labels"]) == set(app.task_or_404("figure_kind").labels)
    assert "Obr. / Obrázok (cs Obrázek)" in first.guide["slovak_glossary"]
    assert first.guide["question"] == "What is this image?"


def test_the_unique_lease_is_the_lock(agent_services: Services, monkeypatch: pytest.MonkeyPatch):
    """Two claims that read the same candidates at once: the second insert loses, not duplicates."""
    app.sample(agent_services, "figure_kind", 3)
    won = app.claim(agent_services, "figure_kind", 1, "a1", MODEL)
    monkeypatch.setattr(app, "_leased", lambda *a, **k: set())   # b1 did not see a1's lease yet
    lost = app.claim(agent_services, "figure_kind", 1, "b1", MODEL)
    assert lost.items and lost.items[0].id != won.items[0].id


def test_an_expired_lease_is_offered_again(agent_services: Services):
    from evilflowers_books_digitalizer.studio.application.uow import LabelLease, select

    app.sample(agent_services, "figure_kind", 2)
    held = app.claim(agent_services, "figure_kind", 5, "a1", MODEL)
    assert len(held.items) == 2 and not app.claim(agent_services, "figure_kind", 5, "b1", MODEL).items
    with agent_services.uow() as uow:
        for lease in uow.session.scalars(select(LabelLease)):
            lease.expires_at = datetime(2000, 1, 1)
    assert len(app.claim(agent_services, "figure_kind", 5, "b1", MODEL).items) == 2


def test_claimed_items_never_carry_the_models_answer(agent_services: Services):
    for t in ("figure_kind", "is_data_table", "section_relation", "subject"):
        app.sample(agent_services, t, 10)
        claim = app.claim(agent_services, t, 10, "claude", MODEL).model_dump()
        leaked = _walk_keys(claim["items"]) & set(ANCHORS)
        assert claim["items"] and not leaked, f"{t} claim leaks {leaked}"
        for word in ("review", "prerequisite", "model-written", "strands"):
            assert word not in json.dumps(claim["items"], ensure_ascii=False)


def test_agent_answers_carry_provenance_and_release_the_lease(agent_services: Services):
    from evilflowers_books_digitalizer.studio.application.uow import LabelLease, select

    app.sample(agent_services, "figure_kind", 3)
    claim = app.claim(agent_services, "figure_kind", 3, "claude", MODEL)
    a, b, c = (i.id for i in claim.items)
    out = app.submit_agent(agent_services, "claude", MODEL, [
        app.AgentLabelIn(item_id=a, task="figure_kind", value="chart"),
        app.AgentLabelIn(item_id=b, task="figure_kind", value="cat"),          # not a label
        app.AgentLabelIn(item_id=c, task="figure_kind", value="skip")])
    assert [r["item_id"] for r in out.recorded] == [a, c]
    assert out.rejected[0]["item_id"] == b and "not a figure_kind label" in out.rejected[0]["error"]
    with agent_services.uow() as uow:
        assert {x.item_pk for x in uow.session.scalars(select(LabelLease))} == {b}   # b stays held
    line = next(iter(app.export_lines(agent_services, "figure_kind")))
    assert (line["label"], line["labeller_kind"], line["model"], line["via"]) == ("chart", "agent", MODEL, "mcp")
    # another agent cannot answer an item b is leased to claude for
    other = app.submit_agent(agent_services, "gpt", "x-1", [app.AgentLabelIn(item_id=b, task="figure_kind",
                                                                             value="map")])
    assert other.rejected and "leased to 'claude'" in other.rejected[0]["error"]


def test_a_person_and_an_agent_cannot_share_a_name(agent_services: Services, lab_client: TestClient):
    app.sample(agent_services, "figure_kind", 1)
    item = lab_client.get("/api/v1/labelling/tasks/figure_kind/next").json()       # as jana
    lab_client.post(f"/api/v1/labelling/items/{item['id']}/labels",
                    json={"labels": [{"task": "figure_kind", "value": "map"}]})
    out = app.submit_agent(agent_services, "jana", MODEL, [app.AgentLabelIn(item_id=item["id"],
                                                                           task="figure_kind", value="chart")])
    assert out.rejected and "as a human labeller" in out.rejected[0]["error"]


def test_people_spot_check_agent_labels_and_the_summary_splits_kappa(agent_services: Services,
                                                                    lab_client: TestClient):
    app.sample(agent_services, "figure_kind", 5)
    claim = app.claim(agent_services, "figure_kind", 4, "claude", MODEL)
    answers = ["chart", "photo", "map", "stamp"]
    app.submit_agent(agent_services, "claude", MODEL, [
        app.AgentLabelIn(item_id=i.id, task="figure_kind", value=v) for i, v in zip(claim.items, answers)])
    agent_ids = {i.id for i in claim.items}
    # ceil(10 % of 4) = 1: the first item a person gets is an agent-labelled one
    spot = lab_client.get("/api/v1/labelling/tasks/figure_kind/next").json()
    assert spot["id"] in agent_ids and spot["mine"] == {}            # the agent's answer is not shown
    agent_value = answers[[i.id for i in claim.items].index(spot["id"])]
    lab_client.post(f"/api/v1/labelling/items/{spot['id']}/labels",
                    json={"labels": [{"task": "figure_kind", "value": agent_value}]})
    fresh = lab_client.get("/api/v1/labelling/tasks/figure_kind/next").json()
    assert fresh["id"] not in agent_ids                                # quota met: the unlabelled one

    s = app.summary(agent_services, "figure_kind")
    assert s.by_labeller_kind == {"agent": 4, "human": 1} and s.by_model == {MODEL: 4}
    assert (s.agent_items, s.spot_checked, s.spot_check_target) == (4, 1, 1)
    assert s.agent_human.pairs == 1 and s.agent_human.agreement == 1.0
    assert s.agent_human.confusion == {agent_value: {agent_value: 1}}
    assert set(s.agent_human.by_language) <= {"sk", "en"}
    assert s.human_human.pairs == 0 and s.double == 0                  # an agent is not a second person
    assert s.agent_trusted is None                                    # one pair: no kappa yet

    kinds = {json.loads(x)["labeller_kind"] for x in lab_client.get(
        "/api/v1/labelling/tasks/figure_kind/export", params={"labeller_kind": "agent"}).text.splitlines()}
    assert kinds == {"agent"}
    humans = lab_client.get("/api/v1/labelling/tasks/figure_kind/export", params={"labeller_kind": "human"})
    assert len(humans.text.splitlines()) == 1
    assert lab_client.get("/api/v1/labelling/tasks/figure_kind/export",
                          params={"labeller_kind": "robot"}).status_code == 400


def test_a_person_is_not_offered_an_item_an_agent_holds(agent_services: Services, lab_client: TestClient):
    app.sample(agent_services, "figure_kind", 2)
    app.claim(agent_services, "figure_kind", 2, "claude", MODEL)
    assert lab_client.get("/api/v1/labelling/tasks/figure_kind/next").json() is None


def test_table_claims_take_both_answers_and_subject_options_come_once(agent_services: Services):
    from evilflowers_books_digitalizer.studio.application.errors import BadRequest

    app.sample(agent_services, "is_data_table", 5)
    with pytest.raises(BadRequest, match="together with is_data_table"):
        app.claim(agent_services, "table_quality", 5, "claude", MODEL)
    claim = app.claim(agent_services, "is_data_table", 5, "claude", MODEL)
    assert claim.guide["companion_guide"]["task"] == "table_quality"
    item = claim.items[0]
    assert item.payload["markdown"].startswith("| a |")
    out = app.submit_agent(agent_services, "claude", MODEL, [
        app.AgentLabelIn(item_id=item.id, task="is_data_table", value="yes"),
        app.AgentLabelIn(item_id=item.id, task="table_quality", value="correct")])
    assert len(out.recorded) == 2 and out.released == 1

    app.sample(agent_services, "subject", 5)
    subj = app.claim(agent_services, "subject", 5, "claude", MODEL)
    assert len(subj.items) == 2 and all("options" not in i.payload for i in subj.items)
    assert [o["term"] for o in subj.options_by_catalog[CAT]] == ["004.8", "51"]
    assert {i.payload["options_catalog"] for i in subj.items} == {CAT}
    with pytest.raises(BadRequest, match="model"):
        app.claim(agent_services, "subject", 5, "claude", "")


def test_mcp_agent_flow(agent_services: Services):
    import anyio
    from mcp.types import ImageContent, TextContent

    from evilflowers_books_digitalizer.studio.api.mcp import AGENT_INSTRUCTIONS, build_mcp

    mcp = build_mcp(agent_services)
    assert mcp.instructions == AGENT_INSTRUCTIONS
    app.sample(agent_services, "figure_kind", 3)

    async def flow() -> None:
        names = {t.name for t in await mcp.list_tools()}
        assert {"claim_label_items", "submit_labels", "release_label_items", "next_label_item"} <= names
        content = await mcp.call_tool("claim_label_items", {"task": "figure_kind", "labeller": "claude",
                                                            "model": MODEL, "n": 3})
        texts = [c.text for c in content if isinstance(c, TextContent)]
        head = json.loads(texts[0])
        assert len(head["items"]) == 3 and head["guide"]["labels"]["chart"]
        assert sum(isinstance(c, ImageContent) for c in content) == 3
        assert texts[1] == f"Item {head['items'][0]}:"                    # label, image, then JSON
        assert isinstance(content[2], ImageContent)
        item = json.loads(texts[2])
        assert item["id"] == head["items"][0] and item["image"]["media_type"] == "image/webp"
        assert not (_walk_keys(item) & set(ANCHORS))
        _, out = await mcp.call_tool("submit_labels", {"labeller": "claude", "model": MODEL, "labels": [
            {"item_id": i, "task": "figure_kind", "value": "diagram"} for i in head["items"][:2]]})
        assert len(out["recorded"]) == 2 and out["released"] == 2
        _, rel = await mcp.call_tool("release_label_items", {"labeller": "claude",
                                                              "item_ids": head["items"][2:]})
        assert rel["released"] == 1
        content, _ = await mcp.call_tool("export_labels", {"task": "figure_kind", "labeller_kind": "agent"})
        lines = [json.loads(x) for x in content[0].text.splitlines()]   # type: ignore[union-attr]
        assert {(x["labeller_kind"], x["model"]) for x in lines} == {("agent", MODEL)}

    anyio.run(flow)


def _webp(w: int, h: int) -> bytes:
    import io as _io

    from PIL import Image as PILImage

    buf = _io.BytesIO()
    PILImage.new("RGB", (w, h), "white").save(buf, "WEBP")
    return buf.getvalue()


def test_claim_sends_sized_crops_and_cuts_to_the_budget(agent_services: Services,
                                                       monkeypatch: pytest.MonkeyPatch):
    from evilflowers_books_digitalizer.studio.application.uow import LabelLease, select

    monkeypatch.setitem(globals(), "WEBP", _webp(3000, 2000))     # the fake catalog's crop
    app.sample(agent_services, "is_data_table", 5)
    app.sample(agent_services, "figure_kind", 5)
    claim, images = app.claim_with_images(agent_services, "is_data_table", 5, "claude", MODEL)
    info = claim.items[0].image
    assert info is not None and (info["width"], info["height"]) == (1568, 1045)   # tables: 1568
    assert info["original_width"] == 3000 and images[claim.items[0].id].media_type == "image/png"
    fig, _ = app.claim_with_images(agent_services, "figure_kind", 1, "claude", MODEL)
    assert fig.items[0].image and fig.items[0].image["width"] == 1024             # figures: 1024

    # a budget for about one crop: the first item is kept, the rest released at once
    cfg = dataclasses.replace(agent_services.config.labelling, agent_claim_token_budget=3000)
    tight = dataclasses.replace(agent_services, config=dataclasses.replace(agent_services.config,
                                                                           labelling=cfg))
    cut, _ = app.claim_with_images(tight, "figure_kind", 10, "other", MODEL)
    assert len(cut.items) == 1 and cut.note and "released" in cut.note
    with agent_services.uow() as uow:
        held = [x.item_pk for x in uow.session.scalars(select(LabelLease).where(
            LabelLease.labeller == "other"))]
    assert held == [cut.items[0].id]


def test_release_drops_only_the_named_items(agent_services: Services):
    app.sample(agent_services, "figure_kind", 4)
    mine = app.claim(agent_services, "figure_kind", 2, "claude", MODEL)
    sibling = app.claim(agent_services, "figure_kind", 2, "claude", MODEL)     # same name
    assert app.release(agent_services, "claude", item_ids=[mine.items[0].id]) == 1
    again = app.claim(agent_services, "figure_kind", 5, "claude-2", MODEL)
    assert [i.id for i in again.items] == [mine.items[0].id]                   # sibling's still held
    assert sibling.items[0].id not in {i.id for i in again.items}


def test_agent_trust_waits_for_the_whole_spot_check(agent_services: Services, lab_client: TestClient):
    app.sample(agent_services, "figure_kind", 5)
    for _ in range(3):   # claim 5 in batches, answer all chart
        c = app.claim(agent_services, "figure_kind", 2, "claude", MODEL)
        app.submit_agent(agent_services, "claude", MODEL,
                         [app.AgentLabelIn(item_id=i.id, task="figure_kind", value="chart") for i in c.items])
    assert app.summary(agent_services, "figure_kind").spot_check_target == 1
    spot = lab_client.get("/api/v1/labelling/tasks/figure_kind/next").json()
    lab_client.post(f"/api/v1/labelling/items/{spot['id']}/labels",
                    json={"labels": [{"task": "figure_kind", "value": "map"}]})
    s = app.summary(agent_services, "figure_kind")
    assert s.spot_checked == 1 and s.agent_human.kappa is None and s.agent_trusted is None


def test_fit_image_shrinks_a_large_crop():
    import io as _io

    from PIL import Image as PILImage

    from evilflowers_books_digitalizer.studio.adapters.imaging.crops import fit_image

    buf = _io.BytesIO()
    PILImage.new("RGB", (3000, 1500), "white").save(buf, "WEBP")
    data, media, size, original = fit_image(buf.getvalue(), 1024)
    assert media == "image/png" and size == (1024, 512) and original == (3000, 1500)
    small = _io.BytesIO()
    PILImage.new("RGB", (300, 200), "white").save(small, "WEBP")
    assert fit_image(small.getvalue(), 1024)[1:] == ("image/webp", (300, 200), (300, 200))
