"""Catalog import layer: digitized books -> EvilFlowers Catalog entries.

Mirrors the ``metadata`` and ``covers`` layers. A produced book is mapped to an
:class:`~.mapping.EntryManifest` (a catalog-ready ``Entry`` + PDF acquisition),
serialized as a ``*.entry.json`` sidecar (:mod:`.manifest`), and pushed to a
running catalog by :mod:`.publisher` through ``evilflowers_protocol``'s
:class:`~evilflowers_protocol.client.CatalogClient`.

See ``docs/catalog_import.md`` for the end-to-end flow.
"""

from evilflowers_books_digitalizer.catalog.manifest import (
    manifest_path,
    read_manifest,
    write_manifest,
)
from evilflowers_books_digitalizer.catalog.mapping import (
    EntryAuthor,
    EntryManifest,
    build_manifest,
    normalize_language,
    resolve_language,
    split_author,
    target_catalog,
)
from evilflowers_books_digitalizer.catalog.publisher import (
    CatalogSettings,
    Publisher,
    PublishConfigError,
    export_manifests,
    publish_book,
    publish_books,
    publish_corpus,
    publish_source,
)

__all__ = [
    "CatalogSettings",
    "EntryAuthor",
    "EntryManifest",
    "PublishConfigError",
    "Publisher",
    "build_manifest",
    "export_manifests",
    "manifest_path",
    "normalize_language",
    "publish_book",
    "publish_books",
    "publish_corpus",
    "publish_source",
    "read_manifest",
    "resolve_language",
    "split_author",
    "target_catalog",
    "write_manifest",
]
