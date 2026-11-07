"""FastAPI dependencies: the process-wide Services/state and the operator's name.

There is no login (VPN only); ``X-Operator`` is a free-text name the SPA keeps
per browser and is recorded on notes, reviews, region edits and runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from urllib.parse import unquote

from fastapi import Depends, Header, Request

from evilflowers_books_digitalizer.studio.adapters.imaging.renditions import ImageCache
from evilflowers_books_digitalizer.studio.adapters.imaging.dzi import TileCache
from evilflowers_books_digitalizer.studio.application.fanout import EventFanout
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.ports import IaExporter

MAX_OPERATOR = 100   # Note.author / Region.author column width


@dataclass(slots=True)
class AppState:
    services: Services
    fanout: EventFanout
    ia_exporter: IaExporter
    images: ImageCache
    tiles: TileCache
    ping_seconds: float = 15.0


def app_state(request: Request) -> AppState:
    state: AppState = request.app.state.studio
    return state


def services(state: Annotated[AppState, Depends(app_state)]) -> Services:
    return state.services


def operator(x_operator: Annotated[str | None, Header()] = None) -> str | None:
    """The SPA percent-encodes the name (HTTP headers are Latin-1; Slovak names are
    not), so decode it; plain ASCII names pass through unchanged."""
    name = unquote(x_operator or "").strip()[:MAX_OPERATOR]
    return name or None


State = Annotated[AppState, Depends(app_state)]
Svc = Annotated[Services, Depends(services)]
Operator = Annotated[str | None, Depends(operator)]
