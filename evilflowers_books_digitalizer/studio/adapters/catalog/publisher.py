"""EvilFlowers catalog publisher for the studio (implements ``ports.CatalogPublisher``).

Policy, per call:

1. **Target catalog** = ``PublishConfig.catalog`` formatted with ``collection``.
   The port carries no scan collection, so it comes from
   ``metadata.collection[0]`` (IA's primary collection), else the custom field
   ``evilflowers_collection``.
2. **Create** when ``entry_id is None``. A ``409`` is resolved like the classic
   import (Cookbooks/Digitalizer.md §6) but stricter: an existing entry is
   adopted only when its ISBN equals ours. Its metadata *and files* are not
   overwritten (they may have been curated by a librarian); only formats it
   lacks are uploaded and ``created=False`` is returned. Without an ISBN match
   the call fails with :class:`PublishConflict` and nothing is attached.
3. **Update** (republish) when ``entry_id`` is given: ``PUT`` the full payload,
   then replace the files (4). If the entry was deleted in the catalog, it is
   created again.
4. **Files.** Distribution formats only (``pdf``, ``epub``); the archival
   PDF/A master is never published. Files are uploaded (multipart). On a
   created or updated entry each published format is *replaced*: the new file
   is uploaded first, then every other acquisition of that MIME type on the
   entry is deleted (``DELETE acquisitions/{id}``). Upload-then-delete means a
   failure never leaves the entry without the format; the leftovers of a failed
   call (or a retried POST that attached twice) are removed by the next call.
   Acquisitions of other MIME types are never touched.
5. **No in-place linking.** ``files_base_url`` (``storage_backend=external_url``)
   is refused before any request: the catalog REST API cannot create URL-backed
   acquisitions (docs/design/catalog-protocol-requests.md), and silently
   uploading instead would contradict a config that asked not to.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

from evilflowers_protocol.client import CatalogClient
from evilflowers_protocol.errors import CatalogError, Conflict, NotFound
from evilflowers_protocol.forms import AcquisitionRef, CatalogRef, EntryRef, image_data_uri

from evilflowers_books_digitalizer.catalog.mapping import (
    DEFAULT_LANGUAGE,
    DEFAULT_RELATION,
    EntryManifest,
)
from evilflowers_books_digitalizer.metadata.isbn_lookup import to_isbn13
from evilflowers_books_digitalizer.studio.adapters.catalog import payload
from evilflowers_books_digitalizer.studio.adapters.catalog.client import (
    AcquisitionDeleter,
    StudioCatalogApi,
    TransportDeleter,
)
from evilflowers_books_digitalizer.studio.config import PublishConfig
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.ports import PortError, PublishResult

logger = logging.getLogger(__name__)

#: published format -> MIME; anything else in ``files`` (pdfa, text, …) is not published
MIMES = {"pdf": "application/pdf", "epub": "application/epub+zip"}
#: the catalog rejects entry images above this (``EVILFLOWERS_IMAGE_UPLOAD_MAX_SIZE``)
MAX_COVER_BYTES = 5 * 1024 * 1024


class PublishError(PortError):
    """The catalog refused or failed the publication."""


class PublishConflict(PublishError):
    """409 from a different book (title/ISBN/DOI clash); nothing was attached."""


@dataclass(slots=True)
class EvilFlowersPublisher:
    config: PublishConfig
    #: served root of the output tree; only :meth:`external_url` uses it
    artifacts_root: Path | None = None
    client: StudioCatalogApi | None = None
    #: removes replaced acquisitions; defaults to the client's raw transport
    deleter: AcquisitionDeleter | None = None
    default_language: str = DEFAULT_LANGUAGE
    relation: str = DEFAULT_RELATION

    # -- port -------------------------------------------------------------------
    def publish(self, item_id: str, metadata: ItemMetadata, files: dict[str, Path],
                *, entry_id: str | None = None) -> PublishResult:
        self._refuse_external_url(item_id)
        api = self._api()
        try:
            catalog = api.resolve_catalog(self._target(metadata))
            manifest = self._manifest(metadata, catalog, files)
            image = _cover_uri(files.get("cover"))
            known = _entry_uuid(entry_id)
            if known is None:
                entry, created, adopted = self._create(api, catalog, manifest, image)
            else:
                entry, created, adopted = self._update(api, catalog, known, manifest, image)
            acquisitions = self._attach(api, catalog.id, entry, files, replace=not adopted)
        except CatalogError as exc:
            raise PublishError(f"{item_id}: {exc}") from exc
        return PublishResult(str(entry.id), self._entry_url(catalog, entry.id), created, acquisitions)

    # -- entry ------------------------------------------------------------------
    def _create(self, api: StudioCatalogApi, catalog: CatalogRef, manifest: EntryManifest,
                image: str | None) -> tuple[EntryRef, bool, bool]:
        """(entry, created, adopted)."""
        try:
            created = api.create_entry(catalog.id, payload.entry_create(manifest, image=image))
        except Conflict as exc:
            adopted = self._adopt(api, catalog.id, manifest)
            if adopted is None:
                raise PublishConflict(
                    f"{manifest.slug}: 409 from a different book (no ISBN match); not attaching: {exc}"
                ) from exc
            logger.info("%s: adopted existing entry %s (ISBN match)", manifest.slug, adopted.id)
            return adopted, False, True
        return created, True, False

    def _update(self, api: StudioCatalogApi, catalog: CatalogRef, entry_id: UUID,
                manifest: EntryManifest, image: str | None) -> tuple[EntryRef, bool, bool]:
        """(entry, created, adopted)."""
        try:
            api.update_entry(catalog.id, entry_id, payload.entry_update(manifest, image=image))
        except NotFound:
            logger.warning("%s: entry %s is gone from the catalog; creating it again",
                           manifest.slug, entry_id)
            return self._create(api, catalog, manifest, image)
        return api.get_entry(catalog.id, entry_id), False, False

    def _adopt(self, api: StudioCatalogApi, catalog_id: UUID,
               manifest: EntryManifest) -> EntryRef | None:
        """The same-titled entry whose ISBN equals ours (Digitalizer §6); else None."""
        ours = to_isbn13(manifest.isbn)
        if not ours:
            return None
        for candidate in api.find_entries(catalog_id, title=manifest.title):
            detail = api.get_entry(catalog_id, candidate.id)
            if to_isbn13(detail.identifiers.get("isbn")) == ours:
                return detail
        return None

    # -- files ------------------------------------------------------------------
    def _attach(self, api: StudioCatalogApi, catalog_id: UUID, entry: EntryRef,
                files: dict[str, Path], *, replace: bool) -> dict[str, str]:
        """Upload the distribution formats; ``{format: acquisition id}``.

        ``replace``: upload every format, then delete the entry's other
        acquisitions of the uploaded MIME types. Otherwise (adopted entry) keep
        what is there and upload only the formats it lacks.
        """
        wanted = {fmt: (MIMES[fmt], path) for fmt, path in files.items() if fmt in MIMES}
        existing = {acq.mime: acq for acq in entry.acquisitions}
        out: dict[str, AcquisitionRef] = {}
        for fmt, (mime, path) in wanted.items():
            kept = None if replace else existing.get(mime)
            out[fmt] = kept or api.upload_acquisition(catalog_id, entry.id, path, mime=mime,
                                                      relation=self.relation)
        if replace and out:
            self._drop_replaced(api, catalog_id, entry.id, out)
        return {fmt: str(acq.id) for fmt, acq in out.items()}

    def _drop_replaced(self, api: StudioCatalogApi, catalog_id: UUID, entry_id: UUID,
                       fresh: dict[str, AcquisitionRef]) -> None:
        """Delete every acquisition of a just-uploaded MIME type except the new one.

        Reads the entry *after* the uploads, so it also catches a duplicate a
        retried upload POST attached. Idempotent: a second call finds nothing.
        """
        keep = {acq.id for acq in fresh.values()}
        mimes = {acq.mime for acq in fresh.values()}
        stale = [acq for acq in api.get_entry(catalog_id, entry_id).acquisitions
                 if acq.mime in mimes and acq.id not in keep]
        for acq in stale:
            logger.info("entry %s: deleting replaced %s acquisition %s", entry_id, acq.mime, acq.id)
            self._deleter().delete_acquisition(acq.id)

    def external_url(self, item_id: str, path: Path) -> str:
        """``files_base_url`` + the file's path below the artifacts root
        (``<item_id>/<name>`` when the file lives elsewhere).

        The URL an ``external_url`` acquisition would carry once the catalog can
        create one; unused until then (see :meth:`_refuse_external_url`)."""
        rel: Path
        if self.artifacts_root is not None and path.is_relative_to(self.artifacts_root):
            rel = path.relative_to(self.artifacts_root)
        else:
            rel = Path(item_id) / path.name
        return f"{self.config.files_base_url.rstrip('/')}/{quote(rel.as_posix())}"

    # -- plumbing ---------------------------------------------------------------
    def _target(self, metadata: ItemMetadata) -> str:
        collection = metadata.collection[0] if metadata.collection else \
            next(iter(metadata.extra.get("evilflowers_collection", [])), "")
        try:
            target = self.config.catalog.format(collection=collection, item=metadata.identifier)
        except (KeyError, IndexError) as exc:
            raise PortError(f"bad publish.catalog template {self.config.catalog!r}: {exc}") from exc
        if not target.strip():
            raise PortError(f"{metadata.identifier}: no target catalog (publish.catalog = "
                            f"{self.config.catalog!r} and the item has no collection)")
        return target.strip()

    def _manifest(self, metadata: ItemMetadata, catalog: CatalogRef,
                  files: dict[str, Path]) -> EntryManifest:
        try:
            return payload.to_manifest(
                metadata, catalog=catalog.url_name, default_language=self.default_language,
                relation=self.relation, entry_config=self.config.options.get("entry_config"),
                pdf=files.get("pdf"))
        except ValueError as exc:   # pydantic.ValidationError
            raise PublishError(f"{metadata.identifier}: invalid entry payload: {exc}") from exc

    def _entry_url(self, catalog: CatalogRef, entry_id: UUID) -> str | None:
        """``publish.entry_url`` template (``{catalog}``, ``{catalog_id}``, ``{entry_id}``)."""
        template: Any = self.config.options.get("entry_url")
        if not template:
            return None
        return str(template).format(catalog=catalog.url_name, catalog_id=catalog.id, entry_id=entry_id)

    def _api(self) -> StudioCatalogApi:
        if self.client is None:
            self.client = self._new_client()
        return self.client

    def _deleter(self) -> AcquisitionDeleter:
        if self.deleter is None:
            client = self._api()
            if isinstance(client, CatalogClient):
                self.deleter = TransportDeleter(client)
            elif isinstance(client, AcquisitionDeleter):
                self.deleter = client
            else:
                raise PortError("replacing acquisitions needs a CatalogClient or an explicit deleter")
        return self.deleter

    def _refuse_external_url(self, item_id: str) -> None:
        if self.config.files_base_url:
            raise PortError(
                f"{item_id}: publish.files_base_url is set ({self.config.files_base_url!r}), but the "
                "EvilFlowers catalog API cannot create URL-backed (storage_backend=external_url) "
                "acquisitions yet: AcquisitionMetaForm accepts only relation/prices and "
                "POST /api/v1/acquisitions is not implemented. Remove files_base_url to upload "
                "the files instead; in-place publishing needs the server change in "
                "docs/design/catalog-protocol-requests.md.")

    def _new_client(self) -> CatalogClient:
        if not self.config.base_url:
            raise PortError("publish.base_url is not set")
        token = os.environ.get(self.config.api_key_env)
        if not token:
            raise PortError(f"no catalog API key: set ${self.config.api_key_env}")
        return CatalogClient(self.config.base_url, token)


def _entry_uuid(entry_id: str | None) -> UUID | None:
    """A catalog entry id, or None (create). Ids that are not UUIDs come from
    another target (NullPublisher uses the item identifier) and mean "never
    published here", so a project switching ``none`` -> ``evilflowers`` just creates."""
    if entry_id is None:
        return None
    try:
        return UUID(entry_id)
    except ValueError:
        logger.info("entry id %r is not a catalog UUID; creating a new entry", entry_id)
        return None


def _cover_uri(cover: Path | None) -> str | None:
    """The cover as a data URI, or None when absent / over the catalog's limit."""
    if cover is None or not cover.exists():
        return None
    if cover.stat().st_size > MAX_COVER_BYTES:
        logger.warning("cover %s is over 5 MB; publishing without it", cover)
        return None
    return image_data_uri(cover)
