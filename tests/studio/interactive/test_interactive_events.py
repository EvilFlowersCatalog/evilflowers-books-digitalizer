"""SSE: one bus subscription fanned out to per-client queues, filtered, over HTTP."""

from __future__ import annotations

import asyncio
import threading
import time

from evilflowers_books_digitalizer.studio.adapters.eventbus import InMemoryEventBus
from evilflowers_books_digitalizer.studio.api.events import sse_stream
from evilflowers_books_digitalizer.studio.application.fanout import EventFanout
from evilflowers_books_digitalizer.studio.events import Event, EventType


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def _publish_when_subscribed(bus: InMemoryEventBus, fanout: EventFanout, events: list[Event],
                             clients: int = 1) -> threading.Thread:
    """Publish once the fan-out has its clients and its bus subscription, then end the bus."""
    def run() -> None:
        _wait_until(lambda: fanout.client_count >= clients and bool(bus._subscribers))
        for event in events:
            bus.publish(event)
        bus.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_fanout_filters_per_client():
    bus = InMemoryEventBus()
    fanout = EventFanout(bus)
    events = [Event(EventType.BOOK_READY, "stu", 1), Event(EventType.BOOK_READY, "ku", 2),
              Event(EventType.PAGE_UPDATED, "stu", 3, {"index": 0})]

    async def collect(project, book_id) -> list[tuple[int, Event]]:
        got = []
        with fanout.register(project, book_id) as q:
            while (item := await asyncio.wait_for(q.get(), 5)) is not None:
                got.append(item)
        return got

    async def main():
        tasks = [asyncio.create_task(collect(None, None)), asyncio.create_task(collect("stu", None)),
                 asyncio.create_task(collect(None, 3))]
        await asyncio.sleep(0)
        _publish_when_subscribed(bus, fanout, events, clients=3)
        return await asyncio.gather(*tasks)

    everything, stu, book3 = asyncio.run(main())
    assert [e.book_id for _, e in everything] == [1, 2, 3]
    assert [e.book_id for _, e in stu] == [1, 3]
    assert [e.book_id for _, e in book3] == [3]
    assert [seq for seq, _ in everything] == [1, 2, 3]       # monotonic ids shared by all clients
    assert fanout.client_count == 0


def test_stream_sends_pings_and_closes_with_the_bus():
    bus = InMemoryEventBus()
    fanout = EventFanout(bus)

    async def main() -> list[str]:
        chunks = []
        async for chunk in sse_stream(fanout, None, None, ping_seconds=0.05):
            chunks.append(chunk)
            if chunk == ": ping\n\n":
                _publish_when_subscribed(bus, fanout, [Event(EventType.BOOK_READY, "stu", 1)])
        return chunks

    chunks = asyncio.run(main())
    assert chunks[0] == ": connected\n\n" and ": ping\n\n" in chunks
    assert any(c.startswith("id: 1\nevent: book.ready\ndata: {") for c in chunks)


def test_events_endpoint_streams_matching_events(client, bus):
    fanout = client.app.state.studio.fanout
    _publish_when_subscribed(bus, fanout, [Event(EventType.BOOK_READY, "ku", 2),
                                           Event(EventType.PAGE_UPDATED, "stu", 7, {"index": 3})])
    r = client.get("/api/v1/events", params={"project": "stu"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert "event: page.updated" in body and '"book_id":7' in body
    assert "book.ready" not in body
