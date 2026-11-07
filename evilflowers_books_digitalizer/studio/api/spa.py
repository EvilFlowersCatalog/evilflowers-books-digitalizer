"""Serve the built SPA (``frontend/dist``) with client-side-routing fallback.

Any GET outside ``/api`` returns the file under the static dir when it exists,
otherwise ``index.html`` (React Router owns the URL). Paths are resolved and
checked to stay inside the static dir. A missing build is a plain 404, so the
API runs fine before the SPA is built.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, Response

STATIC_ENV = "STUDIO_STATIC_DIR"


def default_static_dir() -> Path:
    from evilflowers_books_digitalizer.config import PROJECT_ROOT

    return Path(os.environ.get(STATIC_ENV) or PROJECT_ROOT / "frontend" / "dist")


def spa_router(static_dir: Path) -> APIRouter:
    root = static_dir.resolve()
    router = APIRouter()

    @router.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> Response:
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        target = (root / full_path).resolve()
        if full_path and target.is_relative_to(root) and target.is_file():
            return FileResponse(target, headers=_cache_headers(full_path))
        index = root / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": f"SPA not built ({root} has no index.html)"},
                            status_code=404)

    return router


def _cache_headers(path: str) -> dict[str, str]:
    """Vite fingerprints everything under assets/: cache it for good; the rest revalidates."""
    if path.startswith("assets/"):
        return {"Cache-Control": "public, max-age=31536000, immutable"}
    return {"Cache-Control": "no-cache"}
