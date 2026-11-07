"""FastAPI app: ``POST /v1/formulas``, ``GET /healthz``, ``GET /readyz``.

Concurrency: the model runs one crop at a time per process (CPU inference is
compute-bound; two at once only thrash the cores). A request holds the model for
its whole batch; up to ``max_queue`` further requests wait, anything beyond that
gets ``503`` with ``Retry-After`` so the client backs off instead of timing out.

Probes: ``/healthz`` is up as soon as the process is (500 if the model failed to
load, so the kubelet restarts it); ``/readyz`` turns 200 only after the model is in
memory — the load runs in a background thread so the probes answer meanwhile.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import threading
import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from . import recognition as rec
from .engine import Engine

logger = logging.getLogger(__name__)

MAX_ITEMS = 32                      # per request
MAX_IMAGE_BYTES = 8 * 1024 * 1024   # per decoded PNG


class Item(BaseModel):
    key: str = Field(min_length=1, max_length=256)
    image: str = Field(description="base64 PNG of the (padded) region crop")
    task: Literal["formula", "ocr"] = "formula"


class FormulaRequest(BaseModel):
    items: list[Item] = Field(min_length=1, max_length=MAX_ITEMS)


class ItemResult(BaseModel):
    key: str
    text: str | None = None
    error: str | None = None
    secs: float = 0.0


class FormulaResponse(BaseModel):
    model: str
    results: list[ItemResult]


@dataclass
class ServiceState:
    engine: Engine
    max_queue: int
    ready: threading.Event = field(default_factory=threading.Event)
    load_error: str | None = None
    load_secs: float | None = None
    admitted: int = 0               # requests running + waiting
    avg_request_secs: float = 10.0  # moving average, feeds Retry-After
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def load(self) -> None:
        t = time.monotonic()
        try:
            self.engine.load()
        except Exception as exc:  # noqa: BLE001 - surfaced through /healthz and /readyz
            logger.exception("model load failed")
            self.load_error = f"{type(exc).__name__}: {exc}"[:500]
            return
        self.load_secs = round(time.monotonic() - t, 1)
        self.ready.set()
        logger.info("model ready in %.1f s", self.load_secs)

    def retry_after(self) -> str:
        return str(max(1, round(self.avg_request_secs * max(1, self.admitted))))


def _decode(item: Item) -> Image.Image:
    try:
        raw = base64.b64decode(item.image, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image is not valid base64") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"image larger than {MAX_IMAGE_BYTES} bytes")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            # the client normalizes already; doing it again bounds the cost of any caller
            return rec.normalize(im)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"unreadable image: {exc}") from exc


def _run_batch(engine: Engine, items: list[Item]) -> list[ItemResult]:
    out = []
    for item in items:
        t = time.monotonic()
        try:
            text = engine.recognize(_decode(item), item.task)
            out.append(ItemResult(key=item.key, text=text, secs=round(time.monotonic() - t, 2)))
        except (ValueError, RuntimeError) as exc:
            out.append(ItemResult(key=item.key, error=str(exc)[:300],
                                  secs=round(time.monotonic() - t, 2)))
    return out


def create_app(engine: Engine, *, max_queue: int = 2, load_in_background: bool = True) -> FastAPI:
    state = ServiceState(engine=engine, max_queue=max_queue)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if load_in_background:
            threading.Thread(target=state.load, name="model-load", daemon=True).start()
        else:
            state.load()
        yield

    app = FastAPI(title="PaddleOCR-VL formula service", lifespan=lifespan)
    app.state.vlm = state

    def unavailable(detail: str) -> JSONResponse:
        return JSONResponse({"detail": detail}, status_code=503,
                            headers={"Retry-After": state.retry_after()})

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        if state.load_error:
            return JSONResponse({"status": "failed", "error": state.load_error}, status_code=500)
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        if not state.ready.is_set():
            return JSONResponse({"status": "loading", "error": state.load_error}, status_code=503)
        return JSONResponse({"status": "ready", "model": engine.name, "load_secs": state.load_secs,
                             "queue": state.admitted})

    @app.post("/v1/formulas", response_model=FormulaResponse)
    async def formulas(body: FormulaRequest) -> FormulaResponse | JSONResponse:
        if not state.ready.is_set():
            return unavailable("model is loading")
        # running + waiting; checked and bumped without an await in between (one event loop)
        if state.admitted > state.max_queue:
            return unavailable("busy: queue full")
        state.admitted += 1
        try:
            async with state.lock:
                t = time.monotonic()
                results = await asyncio.to_thread(_run_batch, engine, body.items)
                state.avg_request_secs = 0.7 * state.avg_request_secs + 0.3 * (time.monotonic() - t)
        finally:
            state.admitted -= 1
        return FormulaResponse(model=engine.name, results=results)

    return app
