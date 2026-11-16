"""FastAPI application factory of the restoration studio.

    uvicorn evilflowers_books_digitalizer.studio.api.app:create_app --factory

With no arguments the config comes from ``$STUDIO_CONFIG`` (else
``configs/studio.toml``) and the production adapters are wired
(:mod:`.wiring`); tests pass ``services`` built from fakes. Nothing connects to
Postgres or Redis until a request needs it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from evilflowers_books_digitalizer.studio.adapters.imaging.renditions import ImageCache
from evilflowers_books_digitalizer.studio import telemetry
from evilflowers_books_digitalizer.studio.api import errors, events, media, routes
from evilflowers_books_digitalizer.studio.api.deps import AppState
from evilflowers_books_digitalizer.studio.api.mcp import build_mcp
from evilflowers_books_digitalizer.studio.adapters.imaging.dzi import TileCache
from evilflowers_books_digitalizer.studio.api.spa import default_static_dir, spa_router
from evilflowers_books_digitalizer.studio.api.wiring import build_ia_exporter, build_services
from evilflowers_books_digitalizer.studio.ports import IaExporter
from evilflowers_books_digitalizer.studio.application.fanout import EventFanout
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.config import StudioConfig, load_config

API_PREFIX = "/api/v1"
#: streamable-HTTP MCP endpoint (studio.api.mcp); stdio: `evilflowers-digitalizer studio mcp`
MCP_PATH = "/mcp"


def create_app(config: StudioConfig | None = None, *, services: Services | None = None,
               ia_exporter: IaExporter | None = None, static_dir: Path | None = None,
               ping_seconds: float = 15.0) -> FastAPI:
    # tracing first: the SQLAlchemy instrumentation hooks engines created after it (#13)
    telemetry.configure("digitalizer-api")
    if services is None:
        services = build_services(config or load_config())
    config = services.config
    mcp = build_mcp(services, streamable_http_path=MCP_PATH)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():    # the /mcp transport's task group
            yield

    app = FastAPI(title="EvilFlowers Restoration Studio", version="1",
                  openapi_url=f"{API_PREFIX}/openapi.json", docs_url=f"{API_PREFIX}/docs",
                  redoc_url=None, lifespan=lifespan)
    app.state.studio = AppState(
        services=services, fanout=EventFanout(services.bus),
        ia_exporter=ia_exporter or build_ia_exporter(config),
        images=ImageCache(config.cache_dir / "viewer"), tiles=TileCache(config.cache_dir / "dzi"),
        ping_seconds=ping_seconds)
    errors.install(app)
    app.add_api_route("/healthz", _healthz, methods=["GET"], include_in_schema=False)
    # no /metrics here: the ledger gauges come from the orchestrator (studio.ledger_metrics)
    for router in (routes.router, media.router, events.router):
        app.include_router(router, prefix=API_PREFIX)
    # labelling over MCP (#7): the transport's own route at exactly /mcp (a Mount would only
    # match /mcp/…, and a bare /mcp would fall through to the SPA catch-all)
    app.router.routes.extend(mcp.streamable_http_app().routes)
    app.include_router(spa_router(static_dir or default_static_dir()))   # catch-all: last
    telemetry.instrument_fastapi(app)
    return app


async def _healthz() -> dict[str, str]:
    """Liveness/readiness for k8s and the Docker HEALTHCHECK: process up, no DB round
    trip (a Postgres blip must not restart every API pod). ``async``: answered on the
    event loop, so it never queues behind sync handlers waiting for a render slot (#12)."""
    return {"status": "ok"}
