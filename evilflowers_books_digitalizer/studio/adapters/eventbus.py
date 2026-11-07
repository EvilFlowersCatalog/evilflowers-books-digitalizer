"""EventBus adapters: Redis pub/sub (production) and in-memory (tests, single process).

Workers publish; the API holds ONE subscription and fans events out to SSE
clients (``studio.events.Event.matches``). Publishing never raises: progress
notifications must not fail a task when Redis hiccups.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator

from evilflowers_books_digitalizer.studio.events import CHANNEL, Event

logger = logging.getLogger(__name__)


class RedisEventBus:
    def __init__(self, url: str, channel: str = CHANNEL) -> None:
        import redis

        self._redis = redis.Redis.from_url(url)
        self.channel = channel

    def publish(self, event: Event) -> None:
        try:
            self._redis.publish(self.channel, event.to_json())
        except Exception as exc:  # noqa: BLE001 — a notification must never fail work
            logger.warning("event publish failed (%s): %s", event.type, exc)

    def subscribe(self) -> Iterator[Event]:
        """Blocking iterator over events (run it in a thread / executor)."""
        pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(self.channel)
        try:
            for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    yield Event.from_json(message["data"])
                except (ValueError, KeyError) as exc:
                    logger.warning("dropping malformed event: %s", exc)
        finally:
            pubsub.close()


class InMemoryEventBus:
    """Thread-safe fan-out to every subscriber's queue; ``published`` keeps a history."""

    def __init__(self) -> None:
        self.published: list[Event] = []
        self._subscribers: list[queue.Queue[Event | None]] = []
        self._lock = threading.Lock()

    def publish(self, event: Event) -> None:
        with self._lock:
            self.published.append(event)
            subscribers = list(self._subscribers)
        for q in subscribers:
            q.put(event)

    def subscribe(self) -> Iterator[Event]:
        q: queue.Queue[Event | None] = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        try:
            while (event := q.get()) is not None:
                yield event
        finally:
            with self._lock:
                self._subscribers.remove(q)

    def close(self) -> None:
        """End every open subscription (tests)."""
        with self._lock:
            for q in self._subscribers:
                q.put(None)
