"""SSE fan-out: ONE EventBus subscription per process, one asyncio queue per client.

A Redis subscription per browser tab would multiply connections and blocking
threads; instead a single daemon thread consumes ``EventBus.subscribe()`` and
hands each event to the clients whose filter matches (``Event.matches``), on
their own event loop (``call_soon_threadsafe``). The thread starts lazily on the
first client, so building the API never touches Redis.

A slow client never blocks the others: its bounded queue drops events, which is
safe because events are notifications and clients re-read state over REST.
When the bus subscription ends, every client stream is closed (``None``).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from evilflowers_books_digitalizer.studio.events import Event
from evilflowers_books_digitalizer.studio.ports import EventBus

logger = logging.getLogger(__name__)

#: what a client queue carries: (monotonic id, event), or None = stream over
type Item = tuple[int, Event] | None


@dataclass(eq=False, slots=True)
class _Client:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[Item]
    project: str | None
    book_id: int | None
    dropped: int = field(default=0)


class EventFanout:
    def __init__(self, bus: EventBus, max_queue: int = 1000) -> None:
        self._bus = bus
        self._max_queue = max_queue
        self._clients: list[_Client] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ids = itertools.count(1)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._pump, name="studio-sse-fanout", daemon=True)
            self._thread.start()

    @contextmanager
    def register(self, project: str | None = None, book_id: int | None = None
                 ) -> Iterator[asyncio.Queue[Item]]:
        """Subscribe the calling coroutine's loop; unregisters on exit (client gone)."""
        client = _Client(asyncio.get_running_loop(), asyncio.Queue(self._max_queue), project, book_id)
        with self._lock:
            self._clients.append(client)
        self.ensure_started()
        try:
            yield client.queue
        finally:
            with self._lock:
                self._clients.remove(client)

    # ------------------------------------------------------------ pump thread
    def _pump(self) -> None:
        try:
            for event in self._bus.subscribe():
                self._dispatch(next(self._ids), event)
        except Exception:  # noqa: BLE001 — the API must survive a dead broker
            logger.exception("event subscription failed; SSE clients will reconnect")
        finally:
            self._close_all()

    def _dispatch(self, seq: int, event: Event) -> None:
        with self._lock:
            targets = [c for c in self._clients if event.matches(c.project, c.book_id)]
        for client in targets:
            self._deliver(client, (seq, event))

    def _close_all(self) -> None:
        with self._lock:
            targets = list(self._clients)
        for client in targets:
            self._deliver(client, None)

    @staticmethod
    def _deliver(client: _Client, item: Item) -> None:
        try:
            client.loop.call_soon_threadsafe(_offer, client, item)
        except RuntimeError:   # the client's loop is closed: it is going away anyway
            pass


def _offer(client: _Client, item: Item) -> None:
    try:
        client.queue.put_nowait(item)
    except asyncio.QueueFull:
        client.dropped += 1
        if client.dropped == 1:
            logger.warning("SSE client too slow; dropping events (it re-reads over REST)")
