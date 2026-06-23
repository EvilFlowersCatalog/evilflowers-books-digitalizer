"""Read/write the per-book catalog *manifest* sidecar.

A manifest is a small JSON file written next to a book's PDF
(``output/<source>/<slug>.entry.json``) describing the catalog ``Entry`` to
create and the artifacts to attach. It is a durable, reviewable, re-runnable
artifact: digitization produces it (see ``WriteCatalogManifest``), and
publishing consumes it (see :mod:`.publisher`) — the two are decoupled, so an
import can be replayed, audited or handed to a different tool without touching
the scans again.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from evilflowers_books_digitalizer.catalog.mapping import EntryManifest

logger = logging.getLogger(__name__)

#: Sidecar suffix; mirrors the ``.cover.jpg`` / ``.txt`` artifact convention.
MANIFEST_SUFFIX = ".entry.json"


def manifest_path(output_dir: Path, source: str, book_id: str) -> Path:
    """Where a book's manifest lives (sibling of its PDF)."""
    return Path(output_dir) / source / f"{source}_{book_id}{MANIFEST_SUFFIX}"


def write_manifest(manifest: EntryManifest, path: Path) -> Path:
    """Serialize a manifest to ``path`` (pretty JSON, UTF-8). Returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def read_manifest(path: Path) -> EntryManifest:
    """Load a manifest from a sidecar JSON file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return EntryManifest.model_validate(data)


def iter_manifests(output_dir: Path, source: str | None = None) -> Iterator[EntryManifest]:
    """Yield every manifest under ``output_dir`` (optionally one source).

    Malformed sidecars are logged and skipped so one bad file never blocks a
    batch publish.
    """
    root = Path(output_dir) / source if source else Path(output_dir)
    if not root.exists():
        return
    for path in sorted(root.rglob(f"*{MANIFEST_SUFFIX}")):
        try:
            yield read_manifest(path)
        except Exception as exc:  # noqa: BLE001 — one bad sidecar must not block the batch
            logger.warning("skipping malformed manifest %s: %s", path, exc)
