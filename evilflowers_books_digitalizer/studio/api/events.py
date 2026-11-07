"""``GET /api/v1/events``: Server-Sent Events, wire format per ``studio/events.py``.

Plain ``StreamingResponse`` instead of an SSE library: the format is four lines
and the fan-out already exists (:class:`~...application.fanout.EventFanout`).
Starlette cancels the generator when the client disconnects, which unregisters
it from the fan-out.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from evilflowers_books_digitalizer.studio.api.deps import State
from evilflowers_books_digitalizer.studio.application.fanout import EventFanout

router = APIRouter()

_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}   # nginx: do not buffer


@router.get("/events")
async def events(state: State, project: str | None = None, book: int | None = None
                 ) -> StreamingResponse:
    return StreamingResponse(sse_stream(state.fanout, project, book, state.ping_seconds),
                             media_type="text/event-stream", headers=_HEADERS)


async def sse_stream(fanout: EventFanout, project: str | None, book_id: int | None,
                     ping_seconds: float) -> AsyncIterator[str]:
    with fanout.register(project, book_id) as queue:
        yield ": connected\n\n"
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), ping_seconds)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:      # the bus subscription ended: let the client reconnect
                return
            seq, event = item
            yield f"id: {seq}\nevent: {event.type}\ndata: {event.to_json()}\n\n"
