"""Publish digitized books into an EvilFlowers Catalog instance.

The counterpart to ``runner.py`` for the catalog side. HTTP goes through
:class:`evilflowers_protocol.client.CatalogClient`; this module owns the import
*policy*: which catalog a book goes to, how a re-run resumes, and when a 409
means "already ours" rather than "somebody else's book".

Per book (:meth:`Publisher.publish_book`):

1. Resolve the target catalog: the per-book Excel ``cieľový_katalog``, else the
   source key when ``[catalog].by_source = true``, else ``[catalog].catalog``.
2. **Resume.** If the report's last row for the book is ``entry_created`` (or an
   ``error`` that carries an ``entry_id``, i.e. the upload failed), the entry
   exists and only the upload is missing: check it has no PDF yet (a timed-out
   upload may have landed) and go to step 5.
3. ``create_entry`` and **immediately** append an ``entry_created`` row (with
   ``entry_id`` and ``catalog_id``) before uploading.
4. On ``409 Conflict`` look the title up. An entry with the same ISBN (or, when
   neither side has one, exactly the same title) and **no** acquisition is ours
   from an interrupted run: adopt it and go to step 5. If it already has an
   acquisition the book is ``skipped``. Anything else is a ``conflict``; a PDF is
   never attached to somebody else's entry.
5. Upload the **distribution** PDF (the archival master is never uploaded) and
   append ``published``.

Every outcome is appended to ``<output>/publish_report_<source>.jsonl``; the last
row per book wins. ``published``/``skipped`` books are not touched again unless
``force`` (which still never duplicates an entry — step 4 applies). ``dry_run``
builds and validates every payload and writes missing manifests, without HTTP.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from evilflowers_protocol.client import CatalogClient
from evilflowers_protocol.errors import CatalogError, Conflict, NotFound
from evilflowers_protocol.forms import (
    AcquisitionRef,
    CatalogRef,
    EntryCreate,
    EntryRef,
    image_data_uri,
)

from evilflowers_books_digitalizer.catalog.manifest import (
    manifest_path,
    read_manifest,
    write_manifest,
)
from evilflowers_books_digitalizer.catalog.mapping import (
    DEFAULT_LANGUAGE,
    DEFAULT_RELATION,
    EntryManifest,
    build_manifest,
    target_catalog,
)
from evilflowers_books_digitalizer.logs import log_context
from evilflowers_books_digitalizer.metadata.catalog import MetadataCatalog
from evilflowers_books_digitalizer.metadata.draft import derive_isbn
from evilflowers_books_digitalizer.metadata.isbn_lookup import to_isbn13
from evilflowers_books_digitalizer.pipeline.layout import (
    Layout,
    book_dir,
    discover_distribution_pdfs,
    distribution_pdf,
)
from evilflowers_books_digitalizer.runtime import RuntimeConfig, build_catalog, load_runtime

logger = logging.getLogger(__name__)

PUBLISHED = "published"
SKIPPED = "skipped"
ENTRY_CREATED = "entry_created"
CONFLICT = "conflict"
ERROR = "error"
DRY_RUN = "dry-run"

#: report statuses that mean "nothing left to do for this book"
DONE_STATUSES = (PUBLISHED, SKIPPED)

PDF_MIME = "application/pdf"
#: the catalog rejects entry images above this (``EVILFLOWERS_IMAGE_UPLOAD_MAX_SIZE``)
MAX_COVER_BYTES = 5 * 1024 * 1024

_COVER_SUFFIXES = (".cover.jpg", ".cover.jpeg", ".cover.png")


class CatalogApi(Protocol):
    """The part of :class:`~evilflowers_protocol.client.CatalogClient` the publisher uses."""

    def ping(self) -> None: ...

    def resolve_catalog(self, url_name_or_uuid: str) -> CatalogRef: ...

    def create_entry(self, catalog_id: UUID, entry: EntryCreate) -> EntryRef: ...

    def get_entry(self, catalog_id: UUID, entry_id: UUID) -> EntryRef: ...

    def find_entries(self, catalog_id: UUID, *, title: str) -> list[EntryRef]: ...

    def upload_acquisition(
        self,
        catalog_id: UUID,
        entry_id: UUID,
        path: Path,
        *,
        mime: str = ...,
        relation: str = ...,
    ) -> AcquisitionRef: ...


class PublishConfigError(RuntimeError):
    """``[catalog]`` is incomplete (no base URL, no API key)."""


@dataclass
class CatalogSettings:
    """Resolved ``[catalog]`` configuration plus the API key from the environment."""

    enabled: bool
    base_url: str
    catalog: str = ""  # default target (UUID or url_name)
    by_source: bool = False  # target = the source key (one catalog per faculty)
    api_key_env: str = "EVILFLOWERS_API_KEY"
    api_key: str | None = None
    relation: str = DEFAULT_RELATION
    default_language: str = DEFAULT_LANGUAGE
    author_name_order: str = "given_first"
    entry_config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_runtime(cls, rt: RuntimeConfig) -> CatalogSettings:
        cfg = rt.catalog
        api_key_env = cfg.get("api_key_env") or "EVILFLOWERS_API_KEY"
        return cls(
            enabled=bool(cfg.get("enabled", False)),
            base_url=str(cfg.get("base_url", "")).strip(),
            catalog=str(cfg.get("catalog", "") or "").strip(),
            by_source=bool(cfg.get("by_source", False)),
            api_key_env=api_key_env,
            api_key=os.environ.get(api_key_env) or None,
            relation=cfg.get("relation", DEFAULT_RELATION),
            default_language=cfg.get("default_language", DEFAULT_LANGUAGE),
            author_name_order=cfg.get("author_name_order", "given_first"),
            entry_config=dict(cfg.get("entry_config", {})),
        )

    def target_for(self, source: str, override: str | None = None) -> str:
        """The catalog a book of ``source`` imports into (see :func:`target_catalog`)."""
        return target_catalog(
            source, override=override, by_source=self.by_source, default=self.catalog
        )

    def client(self) -> CatalogClient:
        if not self.base_url:
            raise PublishConfigError("[catalog].base_url is not set")
        if not self.api_key:
            raise PublishConfigError(
                f"no API key: set the {self.api_key_env} environment variable"
            )
        return CatalogClient(self.base_url, self.api_key)


def discover_books(output_dir: Path, source: str, layout: Layout) -> list[tuple[str, Path]]:
    """Produced books of ``source`` as ``(book_id, distribution_pdf)`` pairs."""
    prefix = f"{source}_"
    return [
        (slug[len(prefix):], pdf)
        for slug, pdf in discover_distribution_pdfs(Path(output_dir), source, layout)
        if slug.startswith(prefix) and len(slug) > len(prefix)
    ]


def find_cover(directory: Path, slug: str) -> Path | None:
    for suffix in _COVER_SUFFIXES:
        candidate = directory / f"{slug}{suffix}"
        if candidate.exists():
            return candidate
    return None


def load_report(report: Path) -> dict[str, dict[str, Any]]:
    """The last report row per ``book_id`` (malformed lines are ignored)."""
    state: dict[str, dict[str, Any]] = {}
    if not report.exists():
        return state
    for line in report.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("book_id"):
            state[str(row["book_id"])] = row
    return state


def _append(report: Path, row: dict[str, Any]) -> None:
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _pdf_acquisition(entry: EntryRef) -> AcquisitionRef | None:
    return next((acq for acq in entry.acquisitions if acq.mime == PDF_MIME), None)


class Publisher:
    """Publishes produced books of one runtime config; holds one catalog client.

    ``client`` is injectable (tests); by default it is built lazily from
    ``[catalog]`` + the API key, and never in ``dry_run`` mode.
    """

    def __init__(
        self,
        rt: RuntimeConfig,
        *,
        client: CatalogApi | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> None:
        self.rt = rt
        self.settings = CatalogSettings.from_runtime(rt)
        self.layout: Layout = rt.layout
        self.dry_run = dry_run
        self.force = force
        self._client = client
        self._metadata: MetadataCatalog | None = build_catalog(
            rt.metadata, base_dir=rt.config_dir
        )
        self._faculty_names = rt.faculty_names()
        self._reports: dict[str, dict[str, dict[str, Any]]] = {}

    # -- plumbing -------------------------------------------------------------

    @property
    def client(self) -> CatalogApi:
        if self._client is None:
            self._client = self.settings.client()
        return self._client

    def report_path(self, source: str) -> Path:
        return self.rt.output_dir / f"publish_report_{source}.jsonl"

    def _state(self, source: str) -> dict[str, dict[str, Any]]:
        if source not in self._reports:
            self._reports[source] = load_report(self.report_path(source))
        return self._reports[source]

    def _record(self, source: str, row: dict[str, Any]) -> None:
        _append(self.report_path(source), row)
        self._state(source)[row["book_id"]] = row

    # -- manifests ------------------------------------------------------------

    def build_manifest(self, source: str, book_id: str, pdf: Path) -> EntryManifest:
        """A fresh manifest from the Excel row (or a stub) and the on-disk artifacts."""
        faculty = self._faculty_names.get(source, source.upper())
        if self._metadata is not None:
            meta = self._metadata.lookup(book_id, faculty=faculty)
        else:
            meta = MetadataCatalog.stub(book_id, faculty=faculty)
        meta.faculty = meta.faculty or faculty
        if not meta.isbn:  # like ExtractMetadata: an ISBN-named directory is the ISBN
            meta.isbn = derive_isbn(book_id) or None
        directory = book_dir(self.rt.output_dir, source, self.layout)
        return build_manifest(
            meta,
            source=source,
            book_id=book_id,
            pdf=pdf,
            cover=find_cover(directory, f"{source}_{book_id}"),
            catalog=self.settings.catalog,
            by_source=self.settings.by_source,
            default_language=self.settings.default_language,
            relation=self.settings.relation,
            entry_config=self.settings.entry_config,
            author_name_order=self.settings.author_name_order,
        )

    def export_manifest(self, source: str, book_id: str, pdf: Path) -> Path:
        """(Re)build and write a book's manifest sidecar."""
        path = manifest_path(self.rt.output_dir, source, book_id, self.layout)
        return write_manifest(self.build_manifest(source, book_id, pdf), path)

    def manifest_for(self, source: str, book_id: str, pdf: Path) -> EntryManifest:
        """The pipeline-written manifest when present (it carries the OCR/OPAC
        enrichment), else one built and written now."""
        path = manifest_path(self.rt.output_dir, source, book_id, self.layout)
        if not path.exists():
            self.export_manifest(source, book_id, pdf)
        return read_manifest(path)

    def _cover_uri(self, manifest: EntryManifest, directory: Path) -> str | None:
        if not manifest.cover:
            return None
        cover = directory / manifest.cover
        if not cover.exists():
            logger.warning("%s: cover %s missing; publishing without it", manifest.slug, cover)
            return None
        if cover.stat().st_size > MAX_COVER_BYTES:
            logger.warning("%s: cover over 5 MB; publishing without it", manifest.slug)
            return None
        return image_data_uri(cover)

    # -- publishing -----------------------------------------------------------

    def publish_book(self, source: str, book_id: str) -> dict[str, Any]:
        """Publish one produced book; returns its report row (see the module docstring)."""
        slug = f"{source}_{book_id}"
        with log_context(source=source, book_id=book_id, slug=slug, step="publish"):
            row = self._publish(source, book_id)
            level = logging.ERROR if row["status"] == ERROR else (
                logging.WARNING if row["status"] == CONFLICT else logging.INFO
            )
            logger.log(
                level,
                "%s: %s%s", slug, row["status"],
                f" ({row['error']})" if row.get("error") else "",
                extra={k: row[k] for k in ("status", "entry_id", "duration_ms") if k in row},
            )
            return row

    def _publish(self, source: str, book_id: str) -> dict[str, Any]:
        started = time.monotonic()
        base: dict[str, Any] = {"source": source, "book_id": book_id}

        def finish(status: str, **fields: Any) -> dict[str, Any]:
            row = {**base, "status": status, **fields}
            row["duration_ms"] = round((time.monotonic() - started) * 1000)
            if not self.dry_run:
                self._record(source, row)
            return row

        slug = f"{source}_{book_id}"
        pdf = distribution_pdf(self.rt.output_dir, source, slug, self.layout)
        if not pdf.exists():
            return finish(ERROR, error=f"distribution PDF not found: {pdf}")

        try:
            manifest = self.manifest_for(source, book_id, pdf)
            target = self.settings.target_for(source, manifest.catalog_override)
            base["catalog"] = target
            entry = manifest.to_entry_create(image=self._cover_uri(manifest, pdf.parent))
        except (ValueError, OSError) as exc:  # pydantic.ValidationError is a ValueError
            return finish(ERROR, error=f"{type(exc).__name__}: {exc}"[:500])

        if self.dry_run:
            return finish(
                DRY_RUN,
                title=entry.title,
                language_code=entry.language_code,
                isbn=(entry.identifiers or {}).get("isbn"),
                payload_bytes=len(entry.model_dump_json()),
            )

        prior = self._state(source).get(book_id, {})
        if not self.force and prior.get("status") in DONE_STATUSES:
            # already done: report it without appending a duplicate row
            return {
                **base, "status": SKIPPED, "entry_id": prior.get("entry_id"),
                "detail": f"already {prior['status']} (publish report)",
                "duration_ms": round((time.monotonic() - started) * 1000),
            }

        entry_id: UUID | None = None
        try:
            catalog = self.client.resolve_catalog(target)
            base["catalog_id"] = str(catalog.id)

            # An entry exists but its upload never completed: either the run
            # stopped after `entry_created`, or the upload itself failed (`error`
            # with the entry_id). Either way, upload into that entry.
            if prior.get("status") in (ENTRY_CREATED, ERROR) and prior.get("entry_id"):
                entry_id, done = self._resume(catalog, prior)
                if done is not None:
                    return finish(PUBLISHED, **done)

            if entry_id is None:
                try:
                    entry_id = self.client.create_entry(catalog.id, entry).id
                except Conflict as exc:
                    outcome = self._resolve_conflict(catalog, manifest, entry, exc)
                    if outcome["status"] != ENTRY_CREATED:
                        return finish(**outcome)
                    entry_id = UUID(outcome["entry_id"])
                    self._record(source, {**base, **outcome})
                else:
                    self._record(source, {**base, "status": ENTRY_CREATED,
                                          "entry_id": str(entry_id)})

            acquisition = self.client.upload_acquisition(
                catalog.id, entry_id, pdf, mime=PDF_MIME, relation=self.settings.relation
            )
        except PublishConfigError as exc:
            return finish(ERROR, error=str(exc))
        except CatalogError as exc:
            fields: dict[str, Any] = {"error": str(exc)[:500], "http_status": exc.status}
            if entry_id is not None:
                fields["entry_id"] = str(entry_id)
            return finish(ERROR, **fields)

        return finish(
            PUBLISHED,
            entry_id=str(entry_id),
            acquisition_id=str(acquisition.id),
            bytes=pdf.stat().st_size,
        )

    def _resume(
        self, catalog: CatalogRef, prior: dict[str, Any]
    ) -> tuple[UUID | None, dict[str, Any] | None]:
        """Resume a row that holds an ``entry_id``: ``(entry_id, None)`` to upload, or
        ``(…, published fields)`` when the earlier upload landed after all, or
        ``(None, None)`` when the entry is gone and must be created again."""
        if prior.get("catalog_id") and prior["catalog_id"] != str(catalog.id):
            logger.warning("report entry %s is in another catalog; creating anew",
                           prior["entry_id"])
            return None, None
        entry_id = UUID(str(prior["entry_id"]))
        try:
            existing = self.client.get_entry(catalog.id, entry_id)
        except NotFound:
            logger.warning("entry %s from the report no longer exists; creating anew", entry_id)
            return None, None
        acquisition = _pdf_acquisition(existing)
        if acquisition is not None:  # the upload went through before we could record it
            return entry_id, {
                "entry_id": str(entry_id),
                "acquisition_id": str(acquisition.id),
                "detail": "upload had already completed",
            }
        logger.info("resuming upload into entry %s", entry_id)
        return entry_id, None

    def _resolve_conflict(
        self,
        catalog: CatalogRef,
        manifest: EntryManifest,
        entry: EntryCreate,
        exc: Conflict,
    ) -> dict[str, Any]:
        """Decide what a 409 means; returns the fields of the outcome row."""
        ours = to_isbn13(manifest.isbn)
        candidates = self.client.find_entries(catalog.id, title=entry.title)
        for candidate in candidates:
            detail = self.client.get_entry(catalog.id, candidate.id)
            theirs = to_isbn13(detail.identifiers.get("isbn"))
            if ours or theirs:
                same_book = ours == theirs
            else:
                same_book = detail.title.strip() == entry.title.strip()
            if not same_book:
                continue
            if detail.acquisitions:
                return {"status": SKIPPED, "entry_id": str(detail.id),
                        "detail": "already in the catalog with an acquisition"}
            return {"status": ENTRY_CREATED, "entry_id": str(detail.id),
                    "detail": "adopted an existing entry without acquisitions"}
        return {
            "status": CONFLICT,
            "error": str(exc)[:500],
            "detail": "409 from a different book (title, ISBN or DOI clash); not attaching",
            "candidates": [str(c.id) for c in candidates],
        }

    def publish_source(self, source: str, *, limit: int | None = None) -> dict[str, Any]:
        """Publish every produced book of one source; returns a counts/rows summary."""
        books = discover_books(self.rt.output_dir, source, self.layout)
        if limit:
            books = books[:limit]
        started = time.monotonic()
        rows = [self.publish_book(source, book_id) for book_id, _ in books]
        counts = tally(rows)
        logger.info("source %s published in %.1fs: %s",
                    source, time.monotonic() - started, counts)
        return {"source": source, "counts": counts, "rows": rows,
                "report": str(self.report_path(source))}


def tally(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status", ERROR))
        counts[status] = counts.get(status, 0) + 1
    return counts


# -- module-level entry points (CLI) ---------------------------------------------


def export_manifests(
    rt: RuntimeConfig, sources: list[str] | None = None, *, limit: int | None = None
) -> list[Path]:
    """(Re)write manifest sidecars for every produced book (no network calls)."""
    publisher = Publisher(rt, dry_run=True)
    written: list[Path] = []
    for source in sources or rt.source_keys:
        books = discover_books(rt.output_dir, source, publisher.layout)
        if limit:
            books = books[:limit]
        for book_id, pdf in books:
            written.append(publisher.export_manifest(source, book_id, pdf))
    logger.info("wrote %d manifests", len(written))
    return written


def publish_source(
    rt: RuntimeConfig,
    source: str,
    *,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    client: CatalogApi | None = None,
) -> dict[str, Any]:
    """Publish all produced books of one source (see :class:`Publisher`)."""
    publisher = Publisher(rt, client=client, dry_run=dry_run, force=force)
    return publisher.publish_source(source, limit=limit)


def publish_corpus(
    config_path: str | None = None,
    *,
    sources: list[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    client: CatalogApi | None = None,
) -> dict[str, Any]:
    """Publish every configured source into the catalog."""
    rt = load_runtime(config_path)
    publisher = Publisher(rt, client=client, dry_run=dry_run, force=force)
    if not publisher.settings.enabled:
        logger.warning("[catalog] disabled — nothing to publish")
        return {"totals": {}, "sources": {}}
    results = {
        key: publisher.publish_source(key, limit=limit) for key in sources or rt.source_keys
    }
    totals: dict[str, int] = {}
    for res in results.values():
        for status, n in res["counts"].items():
            totals[status] = totals.get(status, 0) + n
    logger.info("corpus published: %s", totals)
    return {"totals": totals, "sources": {k: v["counts"] for k, v in results.items()}}


def publish_books(
    source: str,
    book_ids: list[str],
    *,
    config_path: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    client: CatalogApi | None = None,
) -> list[dict[str, Any]]:
    """Publish the given produced books of one source, in order."""
    publisher = Publisher(load_runtime(config_path), client=client, dry_run=dry_run, force=force)
    return [publisher.publish_book(source, book_id) for book_id in book_ids]


def publish_book(
    source: str,
    book_id: str,
    *,
    config_path: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Publish a single produced book (manifest built on the fly if missing)."""
    return publish_books(
        source, [book_id], config_path=config_path, dry_run=dry_run, force=force
    )[0]
