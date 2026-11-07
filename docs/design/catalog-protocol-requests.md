# Catalog and protocol requests from the studio

Status: **proposed** (2026-11-07). Nothing here is implemented upstream.

The studio publisher (`evilflowers_books_digitalizer/studio/adapters/catalog/`) needs two
things that evilflowers-protocol and the EvilFlowers catalog do not offer today. It
works around the first and refuses the second.

Sources checked: evilflowers-protocol-python at rev `62c05d7` (the pinned one; `main`
at `04ab5cd` adds only CI) and EvilFlowersCatalog/EvilFlowersCatalog `master`.

## A. evilflowers-protocol: acquisition delete / replace wrappers

**Today.** `CatalogClient` (`evilflowers_protocol/client.py`) wraps `ping`,
`resolve_catalog`, `create_entry`, `update_entry`, `get_entry`, `find_entries`,
`upload_acquisition` and `introspect`. It cannot remove an acquisition, although the
catalog can: `DELETE /api/v1/acquisitions/{acquisition_id}` exists, and
`PUT /api/v1/acquisitions/{id}` updates only the acquisition's meta.

**Workaround in the studio.** `studio/adapters/catalog/client.py::delete_acquisition`
sends `client.transport.request("DELETE", f"acquisitions/{id}")` and treats `404` as
already deleted. The transport retries 5xx and timeouts, so a DELETE whose response got
lost comes back as 404 on the retry. The republish is upload-then-delete: upload the new
file, re-read the entry, delete every other acquisition of that MIME type. Re-reading
also catches the duplicate that a retried upload POST can attach (the protocol's own
docstring warns about this).

**Request.** Add to `CatalogClient`:

```python
def delete_acquisition(self, acquisition_id: UUID, *, missing_ok: bool = True) -> None:
    """DELETE acquisitions/{id}; 404 is success when missing_ok."""

def replace_acquisition(self, catalog_id: UUID, entry_id: UUID, path: Path, *,
                        mime: str, relation: str = "open-access") -> AcquisitionRef:
    """Upload ``path``, then delete the entry's other acquisitions of ``mime``.
    Upload first, so a failure never leaves the entry without the format."""
```

Once these are released, the studio drops `delete_acquisition` and `TransportDeleter`
from `client.py` and calls the client directly. No other studio change is needed.

## B. EvilFlowers catalog: create URL-backed acquisitions over REST

**Today.** The `Acquisition` model (`apps/core/models/acquisition.py`) already has
`file_url` and `storage_backend` (`local` | `external_url`). No REST endpoint sets them:

- `POST /api/v1/catalogs/{catalog_id}/entries/{entry_id}` (the acquisition upload) takes
  multipart `content` plus a `metadata` JSON validated by `AcquisitionMetaForm`, which
  has only `relation` and `prices`.
- `POST /api/v1/acquisitions` returns 501 Not implemented.
- Only the internal Dataverse sync (`apps/dataverse/services/sync.py`) creates
  `external_url` acquisitions.

**Why the studio wants it.** The studio writes every book to the NFS volume
(`/data/digitized/studio/<project>/<item_id>/`), which gateway nginx can serve as
`https://elvira.stuba.sk/digitized/...` (`deploy/k8s/README.md`, prerequisite 5). With
URL-backed acquisitions a publish sends a few hundred bytes instead of the PDF and EPUB,
nothing is stored twice, and a republish is an overwrite on the volume.
`EvilFlowersPublisher.external_url()` already builds these URLs:
`files_base_url + "/" + <path below artifacts_root>`.

**Request.** On `POST /api/v1/catalogs/{catalog_id}/entries/{entry_id}`, also accept a
JSON body (no file part) validated by an extended `AcquisitionMetaForm`:

```json
{
  "relation": "open-access",
  "mime": "application/pdf",
  "storage_backend": "external_url",
  "file_url": "https://elvira.stuba.sk/digitized/stu/stu-fei-vektory/stu-fei-vektory.pdf"
}
```

Rules worth agreeing on:

- `file_url` is required when `storage_backend = external_url`. `mime` is required
  because there is no upload to sniff it from.
- Allow-list the URL hosts in settings, because readers' browsers will be sent there.
- Return the same acquisition payload as an upload (`id`, `relation`, `mime`, `url`).
- Serving the acquisition follows whatever the catalog already does for Dataverse
  `external_url` acquisitions (redirect or proxy to `file_url`; not checked here).

The protocol would then add `link_acquisition(catalog_id, entry_id, url, *, mime,
relation)`. The studio would replace its `files_base_url` refusal
(`EvilFlowersPublisher._refuse_external_url`) with a call to it, and the
upload-then-delete replace logic stays the same.

**Until then** a project that sets `publish.files_base_url` fails at publish time,
before any catalog request, with an error that points here. The publisher does not fall
back to uploading, because the setting exists to avoid uploads, and silently uploading
gigabytes would go against what the operator configured. To publish now, remove
`files_base_url` from the project's `[publish]` table.
