"""Publish digitized books into an EvilFlowers Catalog instance.

The counterpart to ``runner.py`` for the catalog side: it walks the produced
output (or its manifest sidecars), pushes each book through
:class:`~evilflowers_books_digitalizer.catalog.client.EvilFlowersClient`, and
appends each result to a resumable ``publish_report_<source>.jsonl``.

Idempotent twice over: books already recorded ``published``/``skipped`` in the
report are skipped locally, and the catalog itself rejects duplicates (409),
which we record as ``skipped``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.catalog.client import CatalogError, EvilFlowersClient
from evilflowers_books_digitalizer.catalog.manifest import (
    manifest_path,
    read_manifest,
    write_manifest,
)
from evilflowers_books_digitalizer.catalog.mapping import build_manifest
from evilflowers_books_digitalizer.metadata.catalog import MetadataCatalog
from evilflowers_books_digitalizer.runtime import RuntimeConfig, build_catalog, load_runtime

logger = logging.getLogger(__name__)

_COVER_SUFFIXES = (".cover.jpg", ".cover.png", ".cover.jpeg")


@dataclass
class CatalogSettings:
    """Resolved ``[catalog]`` configuration plus the API key from the env."""

    enabled: bool
    base_url: str
    catalog: str  # default target (UUID or url_name) for every book
    api_key: str | None
    relation: str = "open-access"
    default_language: str = "slk"
    author_name_order: str = "given_first"
    entry_config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_runtime(cls, rt: RuntimeConfig) -> "CatalogSettings":
        cfg = rt.catalog
        api_key_env = cfg.get("api_key_env", "EVILFLOWERS_API_KEY")
        return cls(
            enabled=cfg.get("enabled", False),
            base_url=cfg.get("base_url", ""),
            catalog=cfg.get("catalog", ""),
            api_key=os.environ.get(api_key_env),
            relation=cfg.get("relation", "open-access"),
            default_language=cfg.get("default_language", "slk"),
            author_name_order=cfg.get("author_name_order", "given_first"),
            entry_config=dict(cfg.get("entry_config", {})),
        )

    def client(self) -> EvilFlowersClient:
        if not self.base_url:
            raise CatalogError("[catalog].base_url is not set")
        if not self.catalog:
            raise CatalogError("[catalog].catalog (target catalog) is not set")
        return EvilFlowersClient(self.base_url, self.api_key or "")


def _find_cover(book_dir: Path, slug: str) -> Path | None:
    for suffix in _COVER_SUFFIXES:
        candidate = book_dir / f"{slug}{suffix}"
        if candidate.exists():
            return candidate
    return None


def discover_books(output_dir: Path, source: str) -> list[tuple[str, Path]]:
    """Find produced books in ``output/<source>`` as ``(book_id, pdf_path)`` pairs."""
    book_dir = Path(output_dir) / source
    if not book_dir.exists():
        return []
    prefix = f"{source}_"
    found: list[tuple[str, Path]] = []
    for pdf in sorted(book_dir.glob(f"{prefix}*.pdf")):
        book_id = pdf.stem[len(prefix):]
        if book_id:
            found.append((book_id, pdf))
    return found


def export_manifest(
    settings: CatalogSettings,
    catalog: MetadataCatalog | None,
    faculty_names: dict[str, str],
    *,
    output_dir: Path,
    source: str,
    book_id: str,
    pdf: Path,
) -> Path:
    """(Re)build and write a manifest sidecar for one already-produced book."""
    faculty = faculty_names.get(source, source.upper())
    if catalog is not None:
        meta = catalog.lookup(book_id, faculty=faculty)
    else:
        meta = MetadataCatalog.stub(book_id, faculty=faculty)
    meta.faculty = meta.faculty or faculty

    manifest = build_manifest(
        meta,
        source=source,
        book_id=book_id,
        pdf=pdf,
        cover=_find_cover(Path(output_dir) / source, f"{source}_{book_id}"),
        catalog=settings.catalog,
        default_language=settings.default_language,
        relation=settings.relation,
        entry_config=settings.entry_config,
        author_name_order=settings.author_name_order,
    )
    return write_manifest(manifest, manifest_path(output_dir, source, book_id))


def export_manifests(
    rt: RuntimeConfig, sources: list[str] | None = None, *, limit: int | None = None
) -> list[Path]:
    """Write manifest sidecars for every produced book (no network calls)."""
    settings = CatalogSettings.from_runtime(rt)
    catalog = build_catalog(rt.metadata)
    faculty_names = rt.faculty_names()
    keys = sources or rt.source_keys
    written: list[Path] = []
    for source in keys:
        books = discover_books(rt.output_dir, source)
        if limit:
            books = books[:limit]
        for book_id, pdf in books:
            written.append(
                export_manifest(
                    settings, catalog, faculty_names,
                    output_dir=rt.output_dir, source=source, book_id=book_id, pdf=pdf,
                )
            )
    logger.info("wrote %d manifests", len(written))
    return written


def _append(report: Path, row: dict[str, Any]) -> None:
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _already_done(report: Path) -> set[str]:
    """book_ids already published/skipped in a prior run (for resumability)."""
    done: set[str] = set()
    if not report.exists():
        return done
    for line in report.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") in ("published", "skipped"):
            done.add(row.get("book_id", ""))
    return done


def publish_source(
    rt: RuntimeConfig,
    source: str,
    *,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    client: EvilFlowersClient | None = None,
) -> dict[str, Any]:
    """Publish all produced books of one faculty; returns a counts/rows summary.

    Manifests are (re)built from the catalog + artifacts on the fly when a
    sidecar is missing, so this works on output produced before the catalog
    layer existed. ``dry_run`` writes manifests and reports what *would* be sent.
    """
    settings = CatalogSettings.from_runtime(rt)
    catalog = build_catalog(rt.metadata)
    faculty_names = rt.faculty_names()
    report = rt.output_dir / f"publish_report_{source}.jsonl"
    done = set() if force else _already_done(report)

    if client is None and not dry_run:
        client = settings.client()

    books = discover_books(rt.output_dir, source)
    if limit:
        books = books[:limit]

    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    for book_id, pdf in books:
        if book_id in done:
            continue
        mpath = manifest_path(rt.output_dir, source, book_id)
        if mpath.exists():
            manifest = read_manifest(mpath)
        else:
            export_manifest(
                settings, catalog, faculty_names,
                output_dir=rt.output_dir, source=source, book_id=book_id, pdf=pdf,
            )
            manifest = read_manifest(mpath)

        if dry_run:
            rows.append({"source": source, "book_id": book_id,
                         "catalog": manifest.catalog, "status": "dry-run"})
            continue

        try:
            row = client.publish_manifest(manifest, output_dir=rt.output_dir)  # type: ignore[union-attr]
        except CatalogError as exc:
            row = {"source": source, "book_id": book_id, "status": "error",
                   "error": str(exc)[:500]}
        rows.append(row)
        _append(report, row)
        logger.info("[%s] %s: %s", source, book_id, row.get("status"))

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    logger.info("source %s published in %.1fs: %s",
                source, time.monotonic() - started, counts)
    return {"source": source, "counts": counts, "rows": rows, "report": str(report)}


def publish_corpus(
    config_path: str | None = None,
    *,
    sources: list[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Publish every configured faculty into the catalog."""
    rt = load_runtime(config_path)
    settings = CatalogSettings.from_runtime(rt)
    if not settings.enabled:
        logger.warning("[catalog] disabled — nothing to publish")
        return {"totals": {}, "sources": {}}
    keys = sources or rt.source_keys
    client = None if dry_run else settings.client()
    results = {
        k: publish_source(rt, k, limit=limit, force=force, dry_run=dry_run, client=client)
        for k in keys
    }
    totals: dict[str, int] = {}
    for res in results.values():
        for status, n in res["counts"].items():
            totals[status] = totals.get(status, 0) + n
    logger.info("corpus published: %s", totals)
    return {"totals": totals, "sources": {k: v["counts"] for k, v in results.items()}}


def publish_book(
    source: str, book_id: str, *, config_path: str | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Publish a single produced book (manifest built on the fly if missing)."""
    rt = load_runtime(config_path)
    settings = CatalogSettings.from_runtime(rt)
    catalog = build_catalog(rt.metadata)
    faculty_names = rt.faculty_names()
    pdf = rt.output_dir / source / f"{source}_{book_id}.pdf"
    if not pdf.exists():
        return {"source": source, "book_id": book_id, "status": "error",
                "error": f"PDF not found: {pdf}"}
    export_manifest(
        settings, catalog, faculty_names,
        output_dir=rt.output_dir, source=source, book_id=book_id, pdf=pdf,
    )
    manifest = read_manifest(manifest_path(rt.output_dir, source, book_id))
    if dry_run:
        return {"source": source, "book_id": book_id,
                "catalog": manifest.catalog, "status": "dry-run"}
    return settings.client().publish_manifest(manifest, output_dir=rt.output_dir)
