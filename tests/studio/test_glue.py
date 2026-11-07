"""Shared glue: fakes satisfy the ports, event bus fan-out, task queue routing, crop forms."""

from __future__ import annotations

import threading

from evilflowers_books_digitalizer.studio import ports
from evilflowers_books_digitalizer.studio.adapters.eventbus import InMemoryEventBus
from evilflowers_books_digitalizer.studio.adapters.queue import CeleryTaskQueue, RecordingTaskQueue
from evilflowers_books_digitalizer.studio.domain import BBox
from evilflowers_books_digitalizer.studio.events import Event, EventType
from evilflowers_books_digitalizer.studio.testing import fakes


def test_fakes_satisfy_ports(tmp_path):
    pairs = [
        (fakes.FakeScanStore(), ports.ScanStore),
        (fakes.FakeArtifactStore(tmp_path), ports.ArtifactStore),
        (fakes.FakePageCleaner(), ports.PageCleaner),
        (fakes.FakeOcr(), ports.OcrEngine),
        (fakes.FakeLayout(), ports.LayoutDetector),
        (fakes.FakeFormulaRecognizer(), ports.FormulaRecognizer),
        (fakes.FakeRenderer(tmp_path), ports.Renderer),
        (fakes.FakePublisher(), ports.CatalogPublisher),
        (fakes.FakeMetadataProvider(), ports.MetadataProvider),
        (InMemoryEventBus(), ports.EventBus),
        (RecordingTaskQueue(), ports.TaskQueue),
    ]
    for impl, port in pairs:
        assert isinstance(impl, port), (type(impl).__name__, port.__name__)


def test_fake_cleaner_plans_then_cleans(tmp_path):
    frames = fakes.make_frames(tmp_path / "scans", 2)
    cleaner = fakes.FakePageCleaner()
    plans = cleaner.plan(frames)
    assert [p.leaf_num for p in plans] == [0, 1, 2, 3] and plans[1].hand_side == "RIGHT"
    pages = cleaner.clean(plans[2:], tmp_path / "out")
    assert [p.leaf_num for p in pages] == [2, 3] and pages[0].image.exists()


def test_in_memory_bus_fans_out():
    bus = InMemoryEventBus()
    got: list[Event] = []
    ready = threading.Event()

    def listen() -> None:
        it = bus.subscribe()
        ready.set()
        got.extend(it)

    t = threading.Thread(target=listen)
    t.start()
    ready.wait(2)
    while not bus._subscribers:    # subscription registers on first next()
        pass
    bus.publish(Event(EventType.BOOK_READY, "stu", 1))
    bus.close()
    t.join(2)
    assert [e.type for e in got] == [EventType.BOOK_READY] and len(bus.published) == 1


def test_queue_routing():
    q = RecordingTaskQueue()
    q.enqueue_run(5, interactive=True)
    q.enqueue_discover("stu", "fei")
    assert q.calls[0] == ("studio.run", (5,), {"interactive": True})
    assert q.calls[1][0] == "studio.discover"

    sent = []

    class FakeApp:
        def send_task(self, name, args, queue, priority):
            sent.append((name, args, queue, priority))
            return type("R", (), {"id": "t1"})()

    cq = CeleryTaskQueue("redis://unused", app=FakeApp())
    assert cq.enqueue_run(7, interactive=True) == "t1"
    cq.enqueue_run(8)
    assert sent == [("studio.run", [7], "interactive", 0), ("studio.run", [8], "io", 5)]


def test_crop_storage_form():
    b = BBox(10, 20, 110, 220)
    assert b.to_xywh() == [10, 20, 100, 200] and BBox.from_xywh(b.to_xywh()) == b


def test_sqlite_session_has_schema():
    from evilflowers_books_digitalizer.db.models import Book, Page, Region

    s = fakes.sqlite_session()
    book = Book(source="fei", book_id="X", slug="fei_X", item_id="stu-fei-x", project="stu",
                artifacts={}, stats={})
    page = Page(index=0, page_type="Cover", add_to_access=True)
    page.regions.append(Region(kind="text", x0=0, y0=0, x1=10, y1=10, source="heuristic",
                               status="proposed", action="keep"))
    book.pages.append(page)
    s.add(book)
    s.commit()
    stored = s.get(Book, book.id)
    assert stored is not None
    assert stored.pages[0].regions[0].kind == "text"
    assert stored.pages[0].leaf_extra == {}
