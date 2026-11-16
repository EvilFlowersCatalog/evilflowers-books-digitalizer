"""Scheduler endpoints (status, pause, resume) and the ledger's /metrics."""

from __future__ import annotations

from evilflowers_books_digitalizer.db.models import Job, Run


def test_scheduler_status_pause_and_resume(client, services):
    s = client.get("/api/v1/scheduler").json()
    assert s["mode"] == "normal" and s["lanes"] == {"interactive": 4, "bulk": 8}
    paused = client.post("/api/v1/scheduler/pause", json={"reason": "catalog migration"}).json()
    assert paused["mode"] == "paused" and paused["reason"] == "catalog migration (jana)"
    assert paused["lanes"]["bulk"] == 0 and paused["lanes"]["interactive"] == 4
    assert client.get("/api/v1/scheduler").json()["mode"] == "paused"   # the switch persists
    assert client.post("/api/v1/scheduler/resume").json()["mode"] == "normal"


def test_metrics_expose_the_ledger(client, services, seeded):
    s = services.sessions()
    run = Run(book_pk=seeded.book_id, kind="full", status="running", log=[])
    s.add(run)
    s.flush()
    s.add_all([Job(run_pk=run.id, stage="clean", seq=i, lane="bulk", backend="local",
                   task="digitalizer.local.clean", queue="digitalizer.cpu", params={},
                   status=status) for i, status in enumerate(("planned", "dispatched"))])
    s.commit()
    s.close()
    r = client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert 'digitalizer_jobs{lane="bulk",stage="clean",status="planned"} 1.0' in body
    assert 'digitalizer_jobs{lane="bulk",stage="clean",status="dispatched"} 1.0' in body
    assert 'digitalizer_runs{kind="full",status="running"} 1.0' in body
    assert 'digitalizer_scheduler_mode{mode="normal"} 1.0' in body
    assert 'digitalizer_scheduler_capacity{lane="bulk"} 8.0' in body
    assert 'digitalizer_job_oldest_planned_seconds{lane="bulk"}' in body
    assert "/metrics" not in client.get("/api/v1/openapi.json").text   # not part of the API
