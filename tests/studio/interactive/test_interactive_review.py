"""The review flow: approve -> render + publish runs (unless the project never publishes)."""

from __future__ import annotations

from evilflowers_books_digitalizer.db.models import Book
from evilflowers_books_digitalizer.studio.events import EventType

from .conftest import seed_book


def _status(services, book_id) -> str:
    s = services.sessions()
    try:
        return s.get(Book, book_id).status
    finally:
        s.close()


def test_approve_enqueues_render_then_publish(client, services, queue, bus, seeded):
    r = client.post(f"/api/v1/books/{seeded.book_id}/reviews",
                    json={"decision": "approved", "note": "regions fixed"})
    assert r.status_code == 201, r.text
    review = r.json()
    assert (review["decision"], review["author"], review["note"]) == ("approved", "jana", "regions fixed")
    assert [run["kind"] for run in review["runs"]] == ["render", "publish"]
    assert all(run["task_id"] for run in review["runs"])
    assert [c[1][0] for c in queue.calls] == [run["id"] for run in review["runs"]]
    assert all(c[2] == {"interactive": False} for c in queue.calls)
    assert _status(services, seeded.book_id) == "approved"
    status_events = [e for e in bus.published if e.type == EventType.BOOK_STATUS]
    assert status_events[-1].data == {"status": "approved", "previous": "review"}
    detail = client.get(f"/api/v1/books/{seeded.book_id}").json()
    assert detail["reviews"][0]["decision"] == "approved" and len(detail["runs"]) == 2


def test_approve_in_a_never_publishing_project_only_records(client, services, queue, tmp_path):
    quiet = seed_book(services, tmp_path, project="quiet", external_id="Q")
    r = client.post(f"/api/v1/books/{quiet.book_id}/reviews", json={"decision": "approved"})
    assert r.status_code == 201 and r.json()["runs"] == [] and queue.calls == []
    assert _status(services, quiet.book_id) == "approved"


def test_approve_without_a_publish_target_only_renders(client, services, queue, tmp_path):
    """Publication is optional: the approved, re-rendered book is finished."""
    files = seed_book(services, tmp_path, project="files", external_id="F")
    r = client.post(f"/api/v1/books/{files.book_id}/reviews", json={"decision": "approved"})
    assert r.status_code == 201, r.text
    assert [run["kind"] for run in r.json()["runs"]] == ["render"]
    assert _status(services, files.book_id) == "approved"
    projects = {p["slug"]: p for p in client.get("/api/v1/projects").json()}
    assert projects["files"]["publishes"] is False and projects["stu"]["publishes"] is True


def test_a_publish_run_needs_a_target(client, services, tmp_path):
    files = seed_book(services, tmp_path, project="files", external_id="F2", status="approved")
    r = client.post(f"/api/v1/books/{files.book_id}/runs", json={"kind": "publish"})
    assert r.status_code == 422 and "no publish target" in r.json()["detail"]


def test_needs_work_and_rejected(client, services, tmp_path, queue):
    approved = seed_book(services, tmp_path, external_id="AP", status="approved")
    r = client.post(f"/api/v1/books/{approved.book_id}/reviews", json={"decision": "needs_work"})
    assert r.status_code == 201 and _status(services, approved.book_id) == "review"
    r = client.post(f"/api/v1/books/{approved.book_id}/reviews", json={"decision": "rejected"})
    assert r.status_code == 201 and _status(services, approved.book_id) == "review"
    assert queue.calls == []


def test_review_conflicts_and_errors(client, services, tmp_path, seeded):
    fresh = seed_book(services, tmp_path, external_id="NEW", status="discovered", pages=False)
    assert client.post(f"/api/v1/books/{fresh.book_id}/reviews",
                       json={"decision": "approved"}).status_code == 409
    assert _status(services, fresh.book_id) == "discovered"          # rolled back, no review row
    assert client.get(f"/api/v1/books/{fresh.book_id}").json()["reviews"] == []
    client.post(f"/api/v1/books/{seeded.book_id}/runs", json={"kind": "render"})
    r = client.post(f"/api/v1/books/{seeded.book_id}/reviews", json={"decision": "approved"})
    assert r.status_code == 409 and _status(services, seeded.book_id) == "review"
    assert client.post("/api/v1/books/999/reviews", json={"decision": "approved"}).status_code == 404
    assert client.post(f"/api/v1/books/{seeded.book_id}/reviews",
                       json={"decision": "maybe"}).status_code == 422
