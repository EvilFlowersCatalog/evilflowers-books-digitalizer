"""Scheduler endpoints (status, pause, resume) and the ledger's /metrics (orchestrator)."""

from __future__ import annotations

import urllib.request

from evilflowers_books_digitalizer.db.models import Job, Run
from evilflowers_books_digitalizer.studio import ledger_metrics


def test_scheduler_status_pause_and_resume(client, services):
    s = client.get("/api/v1/scheduler").json()
    assert s["mode"] == "normal" and s["lanes"] == {"interactive": 4, "bulk": 8}
    paused = client.post("/api/v1/scheduler/pause", json={"reason": "catalog migration"}).json()
    assert paused["mode"] == "paused" and paused["reason"] == "catalog migration (jana)"
    assert paused["lanes"]["bulk"] == 0 and paused["lanes"]["interactive"] == 4
    assert client.get("/api/v1/scheduler").json()["mode"] == "paused"   # the switch persists
    assert client.post("/api/v1/scheduler/resume").json()["mode"] == "normal"


def test_ledger_metrics_are_served_by_the_orchestrator_not_the_portal(client, services, seeded):
    s = services.sessions()
    run = Run(book_pk=seeded.book_id, kind="full", status="running", log=[])
    s.add(run)
    s.flush()
    s.add_all([Job(run_pk=run.id, stage="clean", seq=i, lane="bulk", backend="local",
                   task="digitalizer.local.clean", queue="digitalizer.cpu", params={},
                   status=status) for i, status in enumerate(("planned", "dispatched"))])
    s.commit()
    s.close()
    port = ledger_metrics.serve(0, ledger_metrics.LedgerCollector(services.sessions,
                                                                  services.admission))
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
        assert r.status == 200 and r.headers["content-type"].startswith("text/plain")
        body = r.read().decode()
    assert 'digitalizer_jobs{lane="bulk",stage="clean",status="planned"} 1.0' in body
    assert 'digitalizer_jobs{lane="bulk",stage="clean",status="dispatched"} 1.0' in body
    assert 'digitalizer_runs{kind="full",status="running"} 1.0' in body
    assert 'digitalizer_scheduler_mode{mode="normal"} 1.0' in body
    assert 'digitalizer_scheduler_capacity{lane="bulk"} 8.0' in body
    assert 'digitalizer_job_oldest_planned_seconds{lane="bulk"}' in body
    assert "digitalizer_ledger_scrape_ok 1.0" in body
    # the portal no longer serves them: two sources would double every sum() (#13)
    assert "digitalizer_jobs" not in client.get("/metrics").text


def test_no_metrics_port_no_server(monkeypatch):
    monkeypatch.delenv(ledger_metrics.PORT_ENV, raising=False)
    assert ledger_metrics.serve() is None


def test_an_unreadable_ledger_answers_with_scrape_ok_zero(caplog):
    def broken():
        raise RuntimeError("no such table: jobs")

    port = ledger_metrics.serve(0, ledger_metrics.LedgerCollector(broken, lambda: None))
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
        body = r.read().decode()
    assert "digitalizer_ledger_scrape_ok 0.0" in body and "digitalizer_jobs" not in body
    assert "could not read the ledger" in caplog.text
