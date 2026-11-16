"""Labelling mode (#7): sampling from a fake catalog, serving without anchoring, export, kappa.

The catalog is the real ``CatalogLabelSource`` over an ``httpx.MockTransport`` that answers
like EvilFlowersCatalog develop (709b712), so adapter, use cases and API are tested together.
"""

from __future__ import annotations

import dataclasses
import json
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
    assert set(line) == {"task", "item", "language", "label", "labeller", "labelled_at", "via"}
    assert line["via"] == "spa"
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
