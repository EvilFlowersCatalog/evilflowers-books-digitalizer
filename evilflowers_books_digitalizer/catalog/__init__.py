"""Catalog import layer: digitized books -> EvilFlowers Catalog entries.

Mirrors the ``metadata`` and ``covers`` layers. A produced book is mapped to an
:class:`~.mapping.EntryManifest` (a catalog-ready ``Entry`` + PDF acquisition),
serialized as a ``*.entry.json`` sidecar (:mod:`.manifest`), and pushed to a
running catalog over its REST API (:mod:`.client`, :mod:`.publisher`).

See ``docs/catalog_import.md`` for the end-to-end flow.
"""

from evilflowers_books_digitalizer.catalog.client import CatalogError, EvilFlowersClient
from evilflowers_books_digitalizer.catalog.manifest import (
    manifest_path,
    read_manifest,
    write_manifest,
)
from evilflowers_books_digitalizer.catalog.mapping import (
    EntryAuthor,
    EntryManifest,
    build_manifest,
    resolve_language,
    split_author,
)
from evilflowers_books_digitalizer.catalog.publisher import (
    CatalogSettings,
    export_manifests,
    publish_book,
    publish_corpus,
    publish_source,
)

__all__ = [
    "CatalogError",
    "CatalogSettings",
    "EntryAuthor",
    "EntryManifest",
    "EvilFlowersClient",
    "build_manifest",
    "export_manifests",
    "manifest_path",
    "publish_book",
    "publish_corpus",
    "publish_source",
    "read_manifest",
    "resolve_language",
    "split_author",
    "write_manifest",
]
