"""Thin REST client for an EvilFlowers Catalog instance.

Stdlib ``urllib`` only — no new dependency, matching ``covers/opac.py`` and
``metadata/isbn_lookup.py``. Authenticates with a long-lived API key (a Bearer
JWT issued by the catalog; see ``apps/core/auth.py``) sent as
``Authorization: Bearer <key>``.

Import flow per book (:meth:`publish_manifest`):

1. resolve the target catalog (``url_name`` slug -> UUID, or a UUID as-is),
2. ``POST /catalogs/<id>/entries`` with the metadata + base64 cover image,
3. ``POST /catalogs/<id>/entries/<entry_id>`` (multipart) to attach the PDF.

The catalog rejects a duplicate (same title/ISBN/DOI) with **409**; we surface
that as a ``skipped`` result so re-runs are idempotent.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.catalog.mapping import EntryManifest

logger = logging.getLogger(__name__)

_UUID_LEN = 36


class CatalogError(RuntimeError):
    """A non-recoverable catalog API error (auth, validation, server)."""

    def __init__(self, message: str, *, status: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _encode_multipart(fields: dict[str, str], file_field: str, filename: str,
                      data: bytes, mime: str) -> tuple[bytes, str]:
    """Encode a single-file ``multipart/form-data`` body. Returns (body, content_type)."""
    boundary = f"----efcd{uuid.uuid4().hex}"
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts += [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            b"",
            str(value).encode("utf-8"),
        ]
    parts += [
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode(),
        f"Content-Type: {mime}".encode(),
        b"",
        data,
        f"--{boundary}--".encode(),
        b"",
    ]
    return crlf.join(parts), f"multipart/form-data; boundary={boundary}"


class EvilFlowersClient:
    """Minimal client for the catalog management REST API.

    ``base_url`` is the API root (e.g. ``https://elvira.digital/api/v1`` or
    ``http://localhost:8000/api/v1``).
    """

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 60.0):
        if not api_key:
            raise CatalogError("missing API key (set the configured api_key_env)")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._catalog_cache: dict[str, str] = {}

    # -- low-level request ----------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        body: bytes | None = None,
        content_type: str | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — configured host
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _read_error(exc)
            raise CatalogError(
                f"{method} {path} -> {exc.code}: {_message(detail) or exc.reason}",
                status=exc.code,
                detail=detail,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CatalogError(f"{method} {path} failed: {exc}") from exc

        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None

    # -- resources ------------------------------------------------------------

    def resolve_catalog(self, catalog: str) -> str:
        """Return the catalog UUID for a UUID (as-is) or a ``url_name`` slug."""
        if _is_uuid(catalog):
            return str(catalog)
        if catalog in self._catalog_cache:
            return self._catalog_cache[catalog]
        page = 1
        while True:
            data = self._request("GET", "catalogs", query={"limit": 100, "page": page})
            items = (data or {}).get("items", [])
            for item in items:
                if item.get("url_name") == catalog or item.get("title") == catalog:
                    self._catalog_cache[catalog] = item["id"]
                    return item["id"]
            meta = (data or {}).get("metadata", {})
            if page >= int(meta.get("pages", 1) or 1):
                break
            page += 1
        raise CatalogError(f"catalog {catalog!r} not found on {self.base_url}")

    def create_entry(self, catalog_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Create an entry; returns the created entry object (with ``id``)."""
        data = self._request("POST", f"catalogs/{catalog_id}/entries", json_body=payload)
        return (data or {}).get("response", data) or {}

    def attach_pdf(self, catalog_id: str, entry_id: str, pdf: Path,
                   *, relation: str) -> dict[str, Any]:
        """Attach a PDF acquisition to an entry via multipart upload."""
        pdf = Path(pdf)
        body, content_type = _encode_multipart(
            fields={"metadata": json.dumps({"relation": relation})},
            file_field="content",
            filename=pdf.name,
            data=pdf.read_bytes(),
            mime="application/pdf",
        )
        data = self._request(
            "POST",
            f"catalogs/{catalog_id}/entries/{entry_id}",
            body=body,
            content_type=content_type,
        )
        return (data or {}).get("response", data) or {}

    # -- orchestration --------------------------------------------------------

    def publish_manifest(self, manifest: EntryManifest, *, output_dir: Path) -> dict[str, Any]:
        """Create the entry and attach its PDF for one manifest.

        Returns a plain-dict result row: ``status`` is ``published`` on success,
        ``skipped`` when the catalog reports a duplicate (409). Resolves the PDF
        and cover relative to the manifest's directory under ``output_dir``.
        """
        row: dict[str, Any] = {
            "source": manifest.source,
            "book_id": manifest.book_id,
            "catalog": manifest.catalog,
            "status": "published",
        }
        book_dir = Path(output_dir) / manifest.source
        catalog_id = self.resolve_catalog(manifest.catalog)

        image_data_uri = _cover_data_uri(book_dir / manifest.cover) if manifest.cover else None
        payload = manifest.entry_payload(image_data_uri=image_data_uri)

        try:
            entry = self.create_entry(catalog_id, payload)
        except CatalogError as exc:
            if exc.status == 409:
                logger.info("%s: already in catalog — skipping", manifest.slug)
                return {**row, "status": "skipped", "detail": "already exists (409)"}
            raise

        entry_id = entry.get("id")
        if not entry_id:
            raise CatalogError(f"catalog returned no entry id for {manifest.slug}", detail=entry)
        row["entry_id"] = entry_id
        pdf = book_dir / manifest.pdf
        if not pdf.exists():
            raise CatalogError(f"PDF not found for {manifest.slug}: {pdf}")
        acquisition = self.attach_pdf(catalog_id, str(entry_id), pdf, relation=manifest.relation)
        row["acquisition_id"] = acquisition.get("id")
        logger.info("%s: published entry %s", manifest.slug, entry_id)
        return row


def _cover_data_uri(cover: Path) -> str | None:
    """Read a cover image into a ``data:<mime>;base64,...`` URI, or None if absent."""
    cover = Path(cover)
    if not cover.exists():
        return None
    mime = mimetypes.guess_type(cover.name)[0] or "image/jpeg"
    encoded = base64.b64encode(cover.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _read_error(exc: urllib.error.HTTPError) -> Any:
    try:
        return json.loads(exc.read())
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _message(detail: Any) -> str | None:
    """Best-effort human message from an RFC 7807 / problem+json body."""
    if isinstance(detail, dict):
        return detail.get("title") or detail.get("detail") or detail.get("message")
    return None
