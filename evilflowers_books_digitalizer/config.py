"""Project configuration.

Secrets live in ``credentials.toml`` (gitignored, see ``credentials.example.toml``).
Everything else has sane defaults and can be overridden when constructing
:class:`Settings`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CREDENTIALS_PATH = PROJECT_ROOT / "credentials.toml"


class WebDAVSource(BaseModel):
    """A single WebDAV scan share (one faculty)."""

    name: str
    url: str
    username: str
    password: str
    root: str = ""  # content root on the share, e.g. "SVF-skeny" (NAS system dirs live beside it)

    def __repr__(self) -> str:  # keep passwords out of notebook output
        return f"WebDAVSource(name={self.name!r}, url={self.url!r})"


class Settings(BaseModel):
    """Runtime settings for the digitalization pipeline."""

    sources: dict[str, WebDAVSource]
    cache_dir: Path = Field(default=PROJECT_ROOT / ".cache")
    output_dir: Path = Field(default=PROJECT_ROOT / "output")
    ocr_language: str = "slk"
    ocr_jobs: int | None = None  # None = let OCRmyPDF use all cores


def load_settings(credentials_path: Path | None = None, **overrides) -> Settings:
    """Load settings, reading WebDAV sources from ``credentials.toml``."""
    path = credentials_path or DEFAULT_CREDENTIALS_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — copy credentials.example.toml to credentials.toml "
            "and fill in the WebDAV credentials."
        )
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    sources = {
        key: WebDAVSource(name=key, **values) for key, values in raw.get("sources", {}).items()
    }
    return Settings(sources=sources, **overrides)
