"""Run creation and cancellation: rows, book status, queue routing, events, 404/409/422/503."""

from __future__ import annotations

import pytest

from evilflowers_books_digitalizer.db.models import Book, Run
from evilflowers_books_digitalizer.studio.application import runs
from evilflowers_books_digitalizer.studio.application.dto import RunIn
from evilflowers_books_digitalizer.studio.application.errors import Conflict, Unavailable
from evilflowers_books_digitalizer.studio.domain import RunKind
from evilflowers_books_digitalizer.studio.events import EventType

from .conftest import seed_book


def _book(services, book_id) -> Book:
    s = services.sessions()
    try:
        return s.get(Book, book_id)
    finally:
        s.close()


def test_full_run_queues_book_and_enqueues_on_io(client, services, queue, bus, tmp_path):
    seeded = seed_book(services, tmp_path, status="discovered", pages=False)
    r = client.post(f"/api/v1/books/{seeded.book_id}/runs", json={"kind": "full"})
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["kind"] == "full" and run["status"] == "queued" and run["created_by"] == "jana"
    assert queue.calls == [("studio.run", (run["id"],), {"interactive": False})]
    assert run["task_id"]
    assert _book(services, seeded.book_id).status == "queued"
    types = [e.type for e in bus.published]
    assert EventType.BOOK_QUEUED in types and EventType.BOOK_STATUS in types


def test_rerun_is_interactive_and_pins_pages(client, queue, seeded):
    r = client.post(f"/api/v1/books/{seeded.book_id}/runs", json={"kind": "rerun", "pages": [1, 0, 1]})
    assert r.status_code == 202, r.text
    assert r.json()["pages"] == [0, 1] and r.json()["interactive"] is True
    assert queue.calls[-1] == ("studio.run", (r.json()["id"],), {"interactive": True})


def test_rerun_may_run_beside_a_batch_render_but_not_a_full_run(client, seeded):
    assert client.post(f"/api/v1/books/{seeded.book_id}/runs", json={"kind": "render"}).status_code == 202
    assert client.post(f"/api/v1/books/{seeded.book_id}/runs",
                       json={"kind": "rerun", "pages": [0]}).status_code == 202
    assert client.post(f"/api/v1/books/{seeded.book_id}/runs", json={"kind": "render"}).status_code == 409


def test_overlapping_reruns_conflict_and_disjoint_ones_do_not(client, seeded):
    """A double click on "Re-run page" must not clean the same leaf twice at once."""
    url = f"/api/v1/books/{seeded.book_id}/runs"
    assert client.post(url, json={"kind": "rerun", "pages": [0]}).status_code == 202
    r = client.post(url, json={"kind": "rerun", "pages": [0]})
    assert r.status_code == 409 and "rerun" in r.json()["detail"]
    assert client.post(url, json={"kind": "rerun", "pages": [1]}).status_code == 202


def test_full_run_conflicts_with_an_interactive_rerun(client, seeded):
    url = f"/api/v1/books/{seeded.book_id}/runs"
    assert client.post(url, json={"kind": "rerun", "pages": [0]}).status_code == 202
    assert client.post(url, json={"kind": "full"}).status_code == 409


def test_run_validation(client, services, tmp_path, seeded):
    url = f"/api/v1/books/{seeded.book_id}/runs"
    assert client.post("/api/v1/books/999/runs", json={"kind": "full"}).status_code == 404
    assert client.post(url, json={"kind": "rerun"}).status_code == 422             # pages needed
    assert client.post(url, json={"kind": "rerun", "pages": [7]}).status_code == 422
    assert client.post(url, json={"kind": "full", "pages": [0]}).status_code == 422
    r = client.post(url, json={"kind": "bogus"})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    bare = seed_book(services, tmp_path, external_id="BARE", status="discovered", pages=False)
    assert client.post(f"/api/v1/books/{bare.book_id}/runs",
                       json={"kind": "render"}).status_code == 409                # no pages yet


def test_publish_requires_an_approved_book(client, services, tmp_path, seeded):
    r = client.post(f"/api/v1/books/{seeded.book_id}/runs", json={"kind": "publish"})
    assert r.status_code == 409 and "review" in r.json()["detail"]
    ok = seed_book(services, tmp_path, external_id="APPR", status="approved")
    r = client.post(f"/api/v1/books/{ok.book_id}/runs", json={"kind": "publish"})
    assert r.status_code == 202 and _book(services, ok.book_id).status == "approved"


def test_second_batch_run_conflicts(client, seeded):
    url = f"/api/v1/books/{seeded.book_id}/runs"
    assert client.post(url, json={"kind": "full"}).status_code == 202
    r = client.post(url, json={"kind": "full"})
    assert r.status_code == 409 and "already has run" in r.json()["detail"]


def test_get_and_cancel_run(client, services, queue, bus, tmp_path):
    seeded = seed_book(services, tmp_path, status="discovered", pages=False)
    run = client.post(f"/api/v1/books/{seeded.book_id}/runs", json={"kind": "full"}).json()
    got = client.get(f"/api/v1/runs/{run['id']}")
    assert got.status_code == 200 and got.json()["status"] == "queued" and got.json()["log"] == []
    r = client.post(f"/api/v1/runs/{run['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert queue.revoked == [run["task_id"]]
    assert _book(services, seeded.book_id).status == "discovered"    # nothing was planned yet
    assert bus.published[-1].type in (EventType.RUN_FINISHED, EventType.BOOK_STATUS)
    assert client.post(f"/api/v1/runs/{run['id']}/cancel").status_code == 409
    assert client.get("/api/v1/runs/999").status_code == 404
    assert client.post("/api/v1/runs/999/cancel").status_code == 404


def test_cancel_of_running_book_with_pages_marks_error(services, seeded):
    run = runs.create_run(services, seeded.book_id, RunIn(kind=RunKind.FULL), "op")
    s = services.sessions()
    s.get(Book, seeded.book_id).status = "running"
    s.get(Run, run.id).status = "running"
    s.commit()
    s.close()
    out = runs.cancel_run(services, run.id, "op")
    book = _book(services, seeded.book_id)
    assert out.status == "cancelled" and book.status == "error"
    assert "cancelled by op" in (book.error or "")


def test_broker_failure_fails_the_run(services, queue, seeded):
    def boom(run_id):
        raise ConnectionError("redis down")

    queue.on_run = boom
    with pytest.raises(Unavailable):
        runs.create_run(services, seeded.book_id, RunIn(kind=RunKind.FULL), None)
    s = services.sessions()
    run = s.query(Run).one()
    assert run.status == "failed" and "redis down" in run.error
    assert s.get(Book, seeded.book_id).status == "error"
    s.close()
    with pytest.raises(Conflict):     # the book can be re-queued but not published
        runs.create_run(services, seeded.book_id, RunIn(kind=RunKind.PUBLISH), None)
