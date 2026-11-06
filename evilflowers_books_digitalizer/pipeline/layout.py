"""Output-tree layout for the rendered PDFs.

Two layouts, selected by ``[render] layout`` in the config:

* ``"flat"`` — both PDFs sit next to the sidecars, distinguished by suffix::

      <output_root>/<source>/<slug>.distribution.pdf
      <output_root>/<source>/<slug>.archival.pdf
      <output_root>/<source>/<slug>.txt, .cover.jpg, .entry.json

* ``"split"`` — access copies and preservation masters live in separate top-level
  trees (serve ``distribution/`` from the web, cold-store ``archival/``); the
  sidecars travel with the distribution copy::

      <output_root>/distribution/<source>/<slug>.pdf, .txt, .cover.jpg, .entry.json
      <output_root>/archival/<source>/<slug>.pdf

In ``"split"`` the book's working directory (where sidecars + the distribution
PDF land) is ``<output_root>/distribution/<source>``, so a PDF for profile *p*
goes to ``<output_root>/<p>/<source>``. :func:`book_dir` and :func:`pdf_path`
keep that arithmetic in one place — both the renderer and the batch
resume-sentinel use them so the paths can never drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

Layout = Literal["flat", "split"]
LAYOUTS: tuple[Layout, ...] = ("flat", "split")
#: layout used when ``[render] layout`` is absent
DEFAULT_LAYOUT: Layout = "flat"
#: profile whose PDF + the sidecars define the book's working directory
PRIMARY_PROFILE = "distribution"


def layout_from_config(config: dict[str, Any]) -> Layout:
    """The ``[render] layout`` of a parsed config (default :data:`DEFAULT_LAYOUT`)."""
    value = config.get("render", {}).get("layout", DEFAULT_LAYOUT)
    if value not in LAYOUTS:
        raise ValueError(f"[render] layout must be one of {LAYOUTS}, got {value!r}")
    return cast(Layout, value)


def book_dir(output_root: Path, source: str, layout: Layout) -> Path:
    """The book's working directory (sidecars, cover, primary PDF) for a source."""
    if layout == "split":
        return output_root / PRIMARY_PROFILE / source
    return output_root / source


def _content_root(book_dir: Path, layout: Layout) -> Path:
    """Recover ``output_root`` from a book directory (inverse of :func:`book_dir`)."""
    return book_dir.parent.parent if layout == "split" else book_dir.parent


def pdf_path(book_dir: Path, source: str, slug: str, profile: str, layout: Layout) -> Path:
    """Destination for one profile's PDF, given the book's working directory."""
    if layout == "split":
        return _content_root(book_dir, layout) / profile / source / f"{slug}.pdf"
    return book_dir / f"{slug}.{profile}.pdf"


def distribution_pdf(output_root: Path, source: str, slug: str, layout: Layout) -> Path:
    """Path of the distribution PDF — the batch resume sentinel."""
    return pdf_path(book_dir(output_root, source, layout), source, slug, PRIMARY_PROFILE, layout)


def sidecar_path(output_root: Path, source: str, slug: str, suffix: str, layout: Layout) -> Path:
    """A sidecar next to the distribution PDF, e.g. ``suffix=".entry.json"``."""
    return book_dir(output_root, source, layout) / f"{slug}{suffix}"


def _distribution_suffix(layout: Layout) -> str:
    return ".pdf" if layout == "split" else f".{PRIMARY_PROFILE}.pdf"


def discover_distribution_pdfs(
    output_root: Path, source: str, layout: Layout
) -> list[tuple[str, Path]]:
    """Produced books of one source as sorted ``(slug, distribution_pdf)`` pairs.

    Only distribution copies are returned: in ``"flat"`` the archival PDF sits in
    the same directory as ``<slug>.archival.pdf`` and is skipped; in ``"split"`` it
    lives in another tree.
    """
    directory = book_dir(output_root, source, layout)
    if not directory.is_dir():
        return []
    suffix = _distribution_suffix(layout)
    found: list[tuple[str, Path]] = []
    for pdf in sorted(directory.glob(f"{source}_*{suffix}")):
        slug = pdf.name[: -len(suffix)]
        if slug and pdf == distribution_pdf(output_root, source, slug, layout):
            found.append((slug, pdf))
    return found
