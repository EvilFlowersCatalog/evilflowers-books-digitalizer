"""Error mapping: application errors -> status codes, every body ``{"detail": str}``.

FastAPI reports request validation as a list; the spec promises a string, so it
is flattened here (the structured list stays available under ``errors``).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from evilflowers_books_digitalizer.studio.application.errors import (
    ApplicationError,
    BadRequest,
    Conflict,
    Invalid,
    NotFound,
    Unavailable,
)

_STATUS: dict[type[ApplicationError], int] = {
    NotFound: 404, Conflict: 409, BadRequest: 400, Invalid: 422, Unavailable: 503,
}


def status_of(exc: ApplicationError) -> int:
    for cls in type(exc).__mro__:
        if cls in _STATUS:
            return _STATUS[cls]
    return 400


async def _application_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApplicationError)
    return JSONResponse({"detail": str(exc)}, status_code=status_of(exc))


async def _validation_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = list(exc.errors())
    detail = "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', ()) if p != 'body')}: {e.get('msg')}"
        for e in errors) or "invalid request"
    return JSONResponse({"detail": detail, "errors": _jsonable(errors)}, status_code=422)


def _jsonable(errors: list[dict[str, object]]) -> list[dict[str, object]]:
    """Drop pydantic's ``ctx``/``input`` (may hold exceptions or bytes)."""
    return [{k: v for k, v in e.items() if k in ("loc", "msg", "type")} for e in errors]


def install(app: FastAPI) -> None:
    app.add_exception_handler(ApplicationError, _application_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
