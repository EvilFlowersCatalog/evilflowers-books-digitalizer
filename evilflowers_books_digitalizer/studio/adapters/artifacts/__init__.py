"""ArtifactStore laid out exactly like an Internet Archive "texts" item.

``<artifacts_root>/<project>/<item_id>/`` holds IA-named files (the table in
docs/design/studio-spec.md, "Output tree"), so ``ia upload``, archive-pdf-tools,
hocr-tools and BookReader work on the directory as-is. Studio-only working files
live under ``<item_id>_studio/`` so an IA export can simply skip that folder.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from evilflowers_books_digitalizer.studio.adapters.ia.naming import IA_FILES
from evilflowers_books_digitalizer.studio.config import StudioConfig
from evilflowers_books_digitalizer.studio.ports import ARTIFACT_KINDS, PortError

#: book-level kinds -> file name template ({id} = item identifier). The IA kinds
#: come from ``ia.naming.IA_FILES``, the one spelling of the IA output tree.
_BOOK_FILES: dict[str, str] = {
    **{kind: "{id}" + spec.suffix for kind, spec in IA_FILES.items()},
    "regions_json": "{id}_studio/regions.json",
}
#: per-leaf kinds -> file name template ({leaf} = 0-based leaf number)
_LEAF_FILES: dict[str, str] = {
    "leaf_webp": "{id}_studio/leaves/{leaf:04d}.webp",
    "dzi": "{id}_studio/dzi/{leaf:04d}.dzi",
}


def studio_dir(item_dir: Path, item_id: str) -> Path:
    """The studio-only folder inside an item (excluded from IA exports)."""
    return item_dir / f"{item_id}_studio"


@dataclass(frozen=True, slots=True)
class IaArtifactStore:
    """Pure path mapping: nothing is created until a writer asks for ``item_dir``."""

    root: Path
    project: str

    def item_dir(self, item_id: str) -> Path:
        return self.root / self.project / item_id

    def path(self, item_id: str, kind: str, *, leaf: int | None = None) -> Path:
        if kind not in ARTIFACT_KINDS:
            raise PortError(f"unknown artifact kind {kind!r}")
        if kind in _LEAF_FILES:
            if leaf is None:
                raise PortError(f"artifact kind {kind!r} needs a leaf number")
            name = _LEAF_FILES[kind].format(id=item_id, leaf=leaf)
        else:
            name = _BOOK_FILES[kind].format(id=item_id)
        return self.item_dir(item_id) / name

    def exists(self, item_id: str, kind: str, *, leaf: int | None = None) -> bool:
        return self.path(item_id, kind, leaf=leaf).exists()


def build_artifact_store(config: StudioConfig, project: str) -> IaArtifactStore:
    """The factory the API's composition root looks up (``studio.api.wiring``)."""
    return IaArtifactStore(config.artifacts_root, project)
