"""Shell glue: SPA fallback, error shape, wiring defaults, the ``studio`` CLI group."""

from __future__ import annotations

import json
import zipfile
from typing import Any, cast
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from evilflowers_books_digitalizer import cli
from evilflowers_books_digitalizer.studio.adapters.artifacts import IaArtifactStore
from evilflowers_books_digitalizer.studio.api import wiring
from evilflowers_books_digitalizer.studio.api.app import create_app
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata, Leaf
from evilflowers_books_digitalizer.studio.ports import IaExporter

from .conftest import seed_book


def test_spa_fallback_and_api_404(client, tmp_path):
    assert client.get("/books/3").status_code == 404                     # not built yet
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>")
    (dist / "assets" / "app-1234.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("nope")
    r = client.get("/books/3/pages/1")
    assert r.status_code == 200 and "root" in r.text and r.headers["cache-control"] == "no-cache"
    r = client.get("/assets/app-1234.js")
    assert r.text == "console.log(1)" and "immutable" in r.headers["cache-control"]
    assert "root" in client.get("/../secret.txt").text                     # never escapes dist
    assert client.get("/api/v1/nope").json() == {"detail": "not found"}


def test_validation_errors_are_strings(client, seeded):
    r = client.post(f"/api/v1/pages/{seeded.page_ids[0]}/regions", json={"kind": "text"})
    assert r.status_code == 422 and "bbox" in r.json()["detail"] and r.json()["errors"]


def test_openapi_lists_every_spec_endpoint(client):
    paths = client.get("/api/v1/openapi.json").json()["paths"]
    for path in ("/api/v1/projects", "/api/v1/projects/{project}/imports", "/api/v1/books",
                 "/api/v1/books/{book_id}", "/api/v1/books/{book_id}/metadata",
                 "/api/v1/books/{book_id}/runs", "/api/v1/runs/{run_id}",
                 "/api/v1/runs/{run_id}/cancel", "/api/v1/books/{book_id}/pages",
                 "/api/v1/pages/{page_id}", "/api/v1/pages/{page_id}/regions",
                 "/api/v1/regions/{region_id}", "/api/v1/notes", "/api/v1/books/{book_id}/reviews",
                 "/api/v1/events", "/api/v1/pages/{page_id}/image/{layer}",
                 "/api/v1/pages/{page_id}/dzi", "/api/v1/pages/{page_id}/dzi_files/{level}/{tile}",
                 "/api/v1/books/{book_id}/files/{kind}", "/api/v1/books/{book_id}/export/ia"):
        assert path in paths, path


def test_create_app_without_services_builds_lazy_production_adapters(config, tmp_path):
    app = create_app(config, static_dir=tmp_path / "dist")
    state = app.state.studio
    assert type(state.services.queue).__name__ == "CeleryTaskQueue"
    assert type(state.services.bus).__name__ == "RedisEventBus"
    assert isinstance(state.ia_exporter, IaExporter)
    assert isinstance(state.services.artifacts_for("stu"), IaArtifactStore)
    assert state.services.artifacts_for("unknown") is None
    with TestClient(app) as raw:
        assert raw.get("/healthz").json() == {"status": "ok"}       # no DB, no broker


def test_operator_header_is_percent_decoded(client, seeded):
    r = client.post("/api/v1/notes", json={"book_id": seeded.book_id, "text": "x"},
                    headers={"X-Operator": quote("Jožko Mrkvička")})
    assert r.status_code == 201 and r.json()["author"] == "Jožko Mrkvička"


def test_ia_exporter_satisfies_the_port(tmp_path):
    exporter = wiring.build_ia_exporter(cast(Any, None))
    assert isinstance(exporter, IaExporter)
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF")
    out = exporter.export_item("item-1", ItemMetadata("item-1", title="T"), [Leaf(0)],
                               {"pdf": pdf}, {}, tmp_path / "item-1_ia.zip")
    names = zipfile.ZipFile(out).namelist()
    assert names == ["item-1/item-1_meta.xml", "item-1/item-1_scandata.xml",
                     "item-1/item-1_page_numbers.json", "item-1/item-1.pdf",
                     "item-1/item-1_files.xml"]


# ---------------------------------------------------------------- CLI
@pytest.fixture
def cli_services(services, monkeypatch):
    monkeypatch.setattr(cli, "_studio_services", lambda args: services)
    return services


def test_cli_parses_the_studio_group():
    parser = cli.build_parser()
    args = parser.parse_args(["studio", "run", "stu-fei-x", "--kind", "rerun", "--pages", "1,2"])
    assert (args.studio_command, args.book, args.kind, args.pages) == \
        ("run", "stu-fei-x", "rerun", "1,2")
    for argv in (["studio", "serve", "--port", "9000"], ["studio", "worker", "-Q", "io"],
                 ["studio", "import", "stu", "fei"], ["studio", "export-ia", "1", "--out", "/tmp"],
                 ["studio", "export-training", "stu"], ["studio", "db-upgrade"],
                 ["studio", "migrate"]):
        assert parser.parse_args(argv).func is not None


def test_cli_import_run_and_exports(cli_services, queue, tmp_path, capsys):
    assert cli.main(["studio", "import", "stu", "fei"]) == 0
    assert queue.calls[-1] == ("studio.discover", ("stu", "fei"), {})
    seeded = seed_book(cli_services, tmp_path)
    assert cli.main(["studio", "run", "stu-fei-book1", "--kind", "rerun", "--pages", "0",
                     "--operator", "cli"]) == 0
    assert queue.calls[-1][2] == {"interactive": True}
    capsys.readouterr()
    assert cli.main(["studio", "export-ia", str(seeded.book_id), "--out", str(tmp_path / "ia")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["zip"].endswith("stu-fei-book1_ia.zip")
    assert cli.main(["studio", "export-training", "stu"]) == 0
    assert queue.calls[-1] == ("studio.export_training", ("stu",), {})
    assert cli.main(["studio", "run", "no-such-book"]) == 1
    assert "not found" in capsys.readouterr().err


def test_cli_worker_execs_celery(monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr("os.execv", lambda exe, argv: seen.update(exe=exe, argv=argv))
    assert cli.main(["studio", "worker", "-Q", "interactive,cpu", "--concurrency", "2"]) == 0
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[1:5] == ["-m", "celery", "-A", cli.STUDIO_CELERY_APP]
    assert argv[5:9] == ["worker", "-Q", "interactive,cpu", "--concurrency"]


def test_cli_worker_strips_separator_before_celery_args(monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr("os.execv", lambda exe, argv: seen.update(exe=exe, argv=argv))
    assert cli.main(["studio", "worker", "-Q", "io", "--", "--pool", "threads"]) == 0
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[-2:] == ["--pool", "threads"]
    assert "--" not in argv
