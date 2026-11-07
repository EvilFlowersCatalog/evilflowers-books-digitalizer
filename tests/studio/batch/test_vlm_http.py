"""HttpFormulaRecognizer against httpx.MockTransport, and the vlm service with a stub engine."""

from __future__ import annotations

import base64
import io
import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from evilflowers_books_digitalizer.studio.adapters import registry
from evilflowers_books_digitalizer.studio.adapters.formula.http import (
    HttpFormulaRecognizer,
    HttpVlmSettings,
)
from evilflowers_books_digitalizer.studio.config import ProjectConfig, StudioConfig
from evilflowers_books_digitalizer.studio.domain import BBox
from evilflowers_books_digitalizer.studio.ports import FormulaJob, PortError
from evilflowers_books_digitalizer.vlm_service import recognition as rec
from evilflowers_books_digitalizer.vlm_service.app import create_app

DRAWING = r"\begin{array}{c}A\\B\end{array}\quad\begin{array}{c}C\\D\end{array}"


@pytest.fixture
def page(tmp_path: Path) -> Path:
    path = tmp_path / "0001.tif"
    Image.new("RGB", (400, 300), "white").save(path)
    return path


def _jobs(page: Path, n: int, task: str = "formula") -> list[FormulaJob]:
    return [FormulaJob(f"region-{i}", page, BBox(10 * i, 10, 10 * i + 60, 40), task) for i in range(n)]


def _recognizer(handler: Any, **kw: Any) -> tuple[HttpFormulaRecognizer, list[float]]:
    slept: list[float] = []
    settings = HttpVlmSettings(url="http://vlm:8000", **kw)
    return (HttpFormulaRecognizer(settings, transport=httpx.MockTransport(handler),
                                  sleep=slept.append), slept)


def _ok(request: httpx.Request, text: str = r"\[x^2\]") -> httpx.Response:
    items = json.loads(request.content)["items"]
    return httpx.Response(200, json={"model": "m", "results": [
        {"key": it["key"], "text": text, "secs": 1.0} for it in items]})


# ------------------------------------------------------------------ client
def test_batches_crops_and_fixes_latex(page: Path) -> None:
    seen: list[list[dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/formulas"
        seen.append(json.loads(request.content)["items"])
        return _ok(request)

    rec_, _ = _recognizer(handler, batch_size=2)
    out = rec_.recognize(_jobs(page, 5))
    assert [len(b) for b in seen] == [2, 2, 1]
    assert out == {f"region-{i}": "x^2" for i in range(5)}      # delimiters stripped
    # the crop is the sidecar's: bbox + PAD, padded to at least 64 px
    im = Image.open(io.BytesIO(base64.b64decode(seen[0][1]["image"])))   # bbox (10, 10, 70, 40)
    assert im.size == (60 + 2 * rec.PAD, 64)
    assert seen[0][0]["task"] == "formula"


def test_ocr_task_passes_through_unfixed(page: Path) -> None:
    rec_, _ = _recognizer(lambda r: _ok(r, "Ak $x$ je"), batch_size=8)
    out = rec_.recognize(_jobs(page, 1, task="ocr"))
    assert out == {"region-0": "Ak $x$ je"}


def test_retries_503_honouring_retry_after_then_succeeds(page: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"detail": "busy"}, headers={"Retry-After": "7"})
        return _ok(request)

    rec_, slept = _recognizer(handler, backoff_s=1.0)
    assert rec_.recognize(_jobs(page, 1)) == {"region-0": "x^2"}
    assert slept == [7.0, 7.0]       # Retry-After wins over the 1 s / 2 s backoff
    assert rec_.settings.max_backoff_s == 60.0


def test_connection_errors_retry_with_backoff_then_raise(page: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    rec_, slept = _recognizer(handler, attempts=4, backoff_s=2.0, max_backoff_s=5.0)
    with pytest.raises(PortError, match="unavailable after 4 attempts"):
        rec_.recognize(_jobs(page, 1))
    assert slept == [2.0, 4.0, 5.0]


def test_read_timeout_drops_the_batch_without_resending(page: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return _ok(request)

    rec_, slept = _recognizer(handler, batch_size=1)
    out = rec_.recognize(_jobs(page, 2))
    assert out == {"region-1": "x^2"} and calls["n"] == 2 and slept == []


def test_partial_failures_and_empty_answers_are_absent(page: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        keys = [it["key"] for it in json.loads(request.content)["items"]]
        return httpx.Response(200, json={"model": "m", "results": [
            {"key": keys[0], "text": "a+b"},
            {"key": keys[1], "error": "RuntimeError: boom"},
            {"key": keys[2], "text": "   "},
            {"key": "region-unknown", "text": "z"}]})

    rec_, _ = _recognizer(handler, batch_size=3)
    assert rec_.recognize(_jobs(page, 3)) == {"region-0": "a+b"}


def test_hallucination_is_counted_not_dropped(page: Path) -> None:
    """The stage's judge() must see it to record the rejection for the quality gate."""
    from evilflowers_books_digitalizer.studio.workers.stages.formulas import REJECTED, judge

    rec_, _ = _recognizer(lambda r: _ok(r, DRAWING))
    out = rec_.recognize(_jobs(page, 1))
    assert out == {"region-0": DRAWING}
    reason = judge(out["region-0"])[1]
    assert reason is not None and reason.startswith(REJECTED)


def test_unexpected_status_raises(page: Path) -> None:
    rec_, _ = _recognizer(lambda r: httpx.Response(422, json={"detail": "bad"}))
    with pytest.raises(PortError, match="422"):
        rec_.recognize(_jobs(page, 1))


def test_unreadable_page_is_skipped(tmp_path: Path, page: Path) -> None:
    jobs = [FormulaJob("gone", tmp_path / "missing.tif", BBox(0, 0, 10, 10), "formula"),
            *_jobs(page, 1)]
    rec_, _ = _recognizer(_ok)
    assert rec_.recognize(jobs) == {"region-0": "x^2"}


def test_registry_builds_http_recognizer(tmp_path: Path) -> None:
    project = ProjectConfig(slug="p", name="P", scans_root=tmp_path,
                            formula_recognizer="paddleocr-vl-http",
                            pipeline={"vlm": {"url": "http://vlm:8000/", "batch_size": 3}})
    cfg = StudioConfig(database_url="sqlite://", redis_url="redis://x", artifacts_root=tmp_path,
                       cache_dir=tmp_path, projects={"p": project})
    built = registry.FORMULA_RECOGNIZERS["paddleocr-vl-http"](cfg, project)
    assert isinstance(built, HttpFormulaRecognizer)
    assert built.settings.url == "http://vlm:8000" and built.settings.batch_size == 3
    with pytest.raises(PortError, match="url is required"):
        registry.FORMULA_RECOGNIZERS["paddleocr-vl-http"](
            cfg, ProjectConfig(slug="p", name="P", scans_root=tmp_path, pipeline={"vlm": {}}))


# ------------------------------------------------------------------ service
class StubEngine:
    name = "stub"

    def __init__(self, gate: threading.Event | None = None, fail_load: bool = False) -> None:
        self.gate = gate
        self.fail_load = fail_load
        self.seen: list[tuple[tuple[int, int], str]] = []

    def load(self) -> None:
        if self.fail_load:
            raise RuntimeError("no weights")

    def recognize(self, image: Image.Image, task: str) -> str:
        if self.gate is not None:
            self.gate.wait(5)
        self.seen.append((image.size, task))
        if image.size[0] == 99:
            raise RuntimeError("model blew up")
        return f"{task}:{image.size[0]}x{image.size[1]}"


def _png(w: int, h: int) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_service_recognizes_with_per_item_errors() -> None:
    engine = StubEngine()
    with TestClient(create_app(engine, load_in_background=False)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json()["status"] == "ready"
        resp = client.post("/v1/formulas", json={"items": [
            {"key": "a", "image": _png(200, 80)},
            {"key": "b", "image": _png(99, 99), "task": "ocr"},
            {"key": "c", "image": "not-base64!!"},
            {"key": "d", "image": base64.b64encode(b"not a png").decode()},
            {"key": "e", "image": _png(3, 140)}]})
    assert resp.status_code == 200
    by_key = {r["key"]: r for r in resp.json()["results"]}
    assert by_key["a"]["text"] == "formula:200x80"
    assert by_key["b"]["error"] == "model blew up"
    assert "base64" in by_key["c"]["error"] and "unreadable" in by_key["d"]["error"]
    assert by_key["e"]["text"] == "formula:64x140"      # normalized again server-side


def test_service_not_ready_until_loaded_and_unhealthy_on_load_failure() -> None:
    with TestClient(create_app(StubEngine(fail_load=True), load_in_background=False)) as client:
        assert client.get("/healthz").status_code == 500
        assert client.get("/readyz").status_code == 503
        resp = client.post("/v1/formulas", json={"items": [{"key": "a", "image": _png(9, 9)}]})
        assert resp.status_code == 503 and "Retry-After" in resp.headers


def test_service_rejects_bad_requests() -> None:
    with TestClient(create_app(StubEngine(), load_in_background=False)) as client:
        assert client.post("/v1/formulas", json={"items": []}).status_code == 422
        too_many = [{"key": str(i), "image": _png(9, 9)} for i in range(33)]
        assert client.post("/v1/formulas", json={"items": too_many}).status_code == 422
        bad_task = [{"key": "a", "image": _png(9, 9), "task": "table"}]
        assert client.post("/v1/formulas", json={"items": bad_task}).status_code == 422


def test_service_queue_limit_returns_503() -> None:
    gate = threading.Event()
    engine = StubEngine(gate=gate)
    body = {"items": [{"key": "a", "image": _png(10, 10)}]}
    with TestClient(create_app(engine, max_queue=1, load_in_background=False)) as client:
        results: list[int] = []
        threads = [threading.Thread(target=lambda: results.append(
            client.post("/v1/formulas", json=body).status_code)) for _ in range(2)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 5
        while client.app.state.vlm.admitted < 2 and time.monotonic() < deadline:  # type: ignore[attr-defined]
            time.sleep(0.01)
        busy = client.post("/v1/formulas", json=body)
        gate.set()
        for t in threads:
            t.join(5)
    assert busy.status_code == 503 and int(busy.headers["Retry-After"]) >= 1
    assert sorted(results) == [200, 200]
