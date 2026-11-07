"""Build an Internet Archive item from a book's data + the artifacts on disk.

The core is plain data (:meth:`IaItemExporter.export_item`, the ``ports.IaExporter``
port): metadata, leaves and file paths in, an IA item zip out. The API and the CLI
use it through the application layer, which reads the rows, so this adapter needs
no database session.

Two row-based conveniences sit on top for the worker shell (it already holds the
rows inside its unit of work):

* :meth:`IaItemExporter.write_item` writes the IA documents (``_meta.xml``,
  ``_scandata.xml``, ``_page_numbers.json``, ``_files.xml``) into the book's
  artifact directory, so the output tree itself *is* an IA item;
* :meth:`IaItemExporter.export_zip` packs one item from rows.

The documents are always generated fresh (the DB is the source of truth after
review edits); binary artifacts (PDF, hOCR, page package …) are copied as they
are. Zip members and ``_files.xml`` entries use IA names from :mod:`.naming`,
whatever the store's own layout is.
"""

from __future__ import annotations

import tempfile
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from evilflowers_books_digitalizer.db.models import Book, Page
from evilflowers_books_digitalizer.studio.adapters.ia import files_xml, meta_xml, page_numbers, scandata
from evilflowers_books_digitalizer.studio.adapters.ia.images import Jp2Options, build_jp2_zip
from evilflowers_books_digitalizer.studio.adapters.ia.leaves import leaves_from_pages
from evilflowers_books_digitalizer.studio.adapters.ia.naming import IA_FILES, ia_name
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata, Leaf
from evilflowers_books_digitalizer.studio.ports import ArtifactStore

#: generated from rows, never copied from disk
GENERATED = ("meta_xml", "scandata_xml", "page_numbers")
#: copied from the store when present
STORED = ("images", "hocr", "pdf", "pdfa", "epub", "text")
#: already-compressed formats: deflating them again only costs CPU
_NO_DEFLATE = frozenset({"images", "pdf", "pdfa", "epub"})


def item_metadata(book: Book, n_leaves: int) -> ItemMetadata:
    """The book's ItemMetadata with the identifier pinned to ``books.item_id``."""
    if not book.item_id:
        raise ValueError(f"book {book.id} has no item_id; it cannot become an IA item")
    meta = ItemMetadata.for_item(book.item_id, book.item_metadata, book.title)
    if meta.imagecount is None:
        meta.imagecount = n_leaves
    return meta


def documents(meta: ItemMetadata, leaves: Sequence[Leaf]) -> dict[str, bytes]:
    """kind -> bytes for the IA documents generated from data (never copied)."""
    return {
        "meta_xml": meta_xml.to_xml(meta),
        "scandata_xml": scandata.to_xml(meta.identifier, leaves, dpi=meta.ppi),
        "page_numbers": page_numbers.to_bytes(meta.identifier, leaves),
    }


@dataclass(slots=True)
class IaItemExporter:
    """``ports.IaExporter``; ``store`` is needed only by the row-based helpers."""

    store: ArtifactStore | None = None
    jp2: Jp2Options = Jp2Options()

    # -- plain data (the port) -----------------------------------------------------
    def export_item(self, item_id: str, metadata: ItemMetadata, leaves: Sequence[Leaf],
                    files: Mapping[str, Path], leaf_images: Mapping[int, Path], out: Path) -> Path:
        """Pack ``<id>/<IA file>`` members; ``_files.xml`` lists every member.

        Without an ``images`` file the page package is built from ``leaf_images``
        when every leaf has one (an IA item's images must cover all leaves).
        """
        meta = ItemMetadata.from_dict({**metadata.as_dict(), "identifier": item_id})
        stored = {k: p for k, p in files.items() if k in STORED and p.exists()}
        with tempfile.TemporaryDirectory(dir=out.parent if out.parent.exists() else None) as tmp:
            if "images" not in stored and leaves and all(lf.leaf_num in leaf_images
                                                         for lf in leaves):
                ordered = [leaf_images[lf.leaf_num] for lf in sorted(leaves, key=lambda x: x.leaf_num)]
                stored["images"] = build_jp2_zip(item_id, ordered, Path(tmp) / "images.zip", self.jp2)
            return _pack(item_id, documents(meta, leaves), stored, out)

    # -- rows (worker shell) -------------------------------------------------------
    def write_item(self, book: Book, pages: Sequence[Page]) -> dict[str, Path]:
        """Write the IA documents into the artifact tree; returns kind -> path."""
        store = self._store()
        leaves = leaves_from_pages(pages)
        meta = item_metadata(book, len(leaves))
        item_id = meta.identifier
        written: dict[str, Path] = {}
        for kind, data in documents(meta, leaves).items():
            path = store.path(item_id, kind)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            written[kind] = path
        entries = [files_xml.describe(p, name=ia_name(item_id, k), source=IA_FILES[k].source,
                                      fmt=IA_FILES[k].format)
                   for k, p in self._present(item_id, GENERATED + STORED)]
        written["files_xml"] = files_xml.write_files_xml(entries, store.path(item_id, "files_xml"))
        return written

    def ensure_images(self, book: Book, pages: Sequence[Page]) -> Path | None:
        """Build ``<id>_jp2.zip`` from the cleaned pages unless it exists; None when
        some leaf has not been cleaned yet (the package must hold every leaf)."""
        store = self._store()
        item_id = item_metadata(book, len(pages)).identifier
        if store.exists(item_id, "images"):
            return store.path(item_id, "images")
        ordered = sorted(pages, key=lambda p: p.index)
        images = [Path(p.clean_path) for p in ordered if p.clean_path]
        if len(images) != len(ordered) or not all(i.exists() for i in images):
            return None
        return build_jp2_zip(item_id, images, store.path(item_id, "images"), self.jp2)

    def export_zip(self, book: Book, pages: Sequence[Page], out: Path, *,
                   build_images: bool = True) -> Path:
        """Pack the item from rows (the store's files are the binary members)."""
        if build_images:
            self.ensure_images(book, pages)
        leaves = leaves_from_pages(pages)
        meta = item_metadata(book, len(leaves))
        return _pack(meta.identifier, documents(meta, leaves),
                     dict(self._present(meta.identifier, STORED)), out)

    def _store(self) -> ArtifactStore:
        if self.store is None:
            raise ValueError("IaItemExporter needs an ArtifactStore for row-based exports")
        return self.store

    def _present(self, item_id: str, kinds: Sequence[str]) -> Iterator[tuple[str, Path]]:
        store = self._store()
        for kind in kinds:
            if store.exists(item_id, kind):
                yield kind, store.path(item_id, kind)


def _pack(item_id: str, docs: Mapping[str, bytes], stored: Mapping[str, Path], out: Path) -> Path:
    """Write the zip: generated documents, then stored files, then ``_files.xml``."""
    out.parent.mkdir(parents=True, exist_ok=True)
    entries: list[files_xml.FileEntry] = []
    tmp = out.with_name(out.name + ".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for kind, data in docs.items():
            zf.writestr(f"{item_id}/{ia_name(item_id, kind)}", data)
            entries.append(files_xml.describe_bytes(data, name=ia_name(item_id, kind),
                                                    source=IA_FILES[kind].source,
                                                    fmt=IA_FILES[kind].format))
        for kind in STORED:
            if (path := stored.get(kind)) is None:
                continue
            name = ia_name(item_id, kind)
            zf.write(path, f"{item_id}/{name}", compress_type=_compression(kind))
            entries.append(files_xml.describe(path, name=name, source=IA_FILES[kind].source,
                                              fmt=IA_FILES[kind].format))
        zf.writestr(f"{item_id}/{ia_name(item_id, 'files_xml')}", files_xml.to_xml(entries))
    tmp.replace(out)
    return out


def _compression(kind: str) -> int:
    return zipfile.ZIP_STORED if kind in _NO_DEFLATE else zipfile.ZIP_DEFLATED
