"""Internet Archive compatibility: IA item documents, export zip, import, upload headers.

The studio's output tree mirrors an IA "texts" item (docs/design/studio-spec.md,
"Internet Archive compatibility"), so this package only translates between our
rows/value objects and IA's file formats:

* :mod:`.meta_xml` — ``<id>_meta.xml`` <-> :class:`ItemMetadata`
* :mod:`.scandata` — ``<id>_scandata.xml`` <-> ``list[Leaf]`` (archive-pdf-tools compatible)
* :mod:`.page_numbers` — ``<id>_page_numbers.json`` (format-version 2, access leaves)
* :mod:`.files_xml` — ``<id>_files.xml`` manifest
* :mod:`.images` — ``<id>_jp2.zip`` page package
* :mod:`.leaves` — ``pages`` rows <-> :class:`Leaf`
* :mod:`.exporter` / :mod:`.importer` — whole items
* :mod:`.headers` — ``ia upload``-compatible IA-S3 headers
"""

from evilflowers_books_digitalizer.studio.adapters.ia.exporter import (
    IaItemExporter,
    documents,
    item_metadata,
)
from evilflowers_books_digitalizer.studio.adapters.ia.headers import meta_headers, upload_headers
from evilflowers_books_digitalizer.studio.adapters.ia.importer import IaItem, read_item
from evilflowers_books_digitalizer.studio.adapters.ia.leaves import (
    apply_leaf,
    leaf_from_page,
    leaves_from_pages,
)
from evilflowers_books_digitalizer.studio.adapters.ia.naming import IA_FILES, ia_name
from evilflowers_books_digitalizer.studio.config import StudioConfig


def build_ia_exporter(config: StudioConfig) -> IaItemExporter:
    """The ``ports.IaExporter`` the API wires in (plain data: no store needed)."""
    return IaItemExporter()


__all__ = [
    "IA_FILES",
    "IaItem",
    "IaItemExporter",
    "apply_leaf",
    "build_ia_exporter",
    "documents",
    "ia_name",
    "item_metadata",
    "leaf_from_page",
    "leaves_from_pages",
    "meta_headers",
    "read_item",
    "upload_headers",
]
