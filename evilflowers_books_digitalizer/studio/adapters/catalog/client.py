"""The slice of the EvilFlowers REST API the studio publisher needs.

:class:`StudioCatalogApi` is structurally satisfied by
:class:`evilflowers_protocol.client.CatalogClient`; tests pass a fake, so no
network is ever needed to exercise the publisher.

evilflowers-protocol cannot delete acquisitions yet, but the catalog can
(``DELETE /api/v1/acquisitions/{id}``). :func:`delete_acquisition` sends that one
request over the client's raw transport; it is the only unwrapped call, kept in
one function so it moves into the protocol unchanged
(docs/design/catalog-protocol-requests.md).

URL-backed acquisitions (``storage_backend=external_url``) are **not** sent: the
catalog's REST API has no way to create them (only its internal Dataverse sync
does), see the same proposal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID

from evilflowers_protocol.client import CatalogClient
from evilflowers_protocol.errors import NotFound
from evilflowers_protocol.forms import (
    AcquisitionRef,
    CatalogRef,
    EntryCreate,
    EntryRef,
    EntryUpdate,
)


class StudioCatalogApi(Protocol):
    def resolve_catalog(self, url_name_or_uuid: str) -> CatalogRef: ...
    def create_entry(self, catalog_id: UUID, entry: EntryCreate) -> EntryRef: ...
    def update_entry(self, catalog_id: UUID, entry_id: UUID, entry: EntryUpdate) -> EntryRef: ...
    def get_entry(self, catalog_id: UUID, entry_id: UUID) -> EntryRef: ...
    def find_entries(self, catalog_id: UUID, *, title: str) -> list[EntryRef]: ...
    def upload_acquisition(self, catalog_id: UUID, entry_id: UUID, path: Path, *,
                           mime: str = ..., relation: str = ...) -> AcquisitionRef: ...


@runtime_checkable
class AcquisitionDeleter(Protocol):
    """Removes one acquisition (and its stored file) from the catalog."""

    def delete_acquisition(self, acquisition_id: UUID) -> None: ...


def delete_acquisition(client: CatalogClient, acquisition_id: UUID) -> None:
    """``DELETE acquisitions/{id}``; an acquisition that is already gone counts as deleted.

    The transport retries 5xx/timeouts, so a DELETE that succeeded on a lost
    response comes back as 404 on the retry; that is success, not an error.
    Candidate for ``CatalogClient.delete_acquisition`` in evilflowers-protocol.
    """
    try:
        client.transport.request("DELETE", f"acquisitions/{acquisition_id}")
    except NotFound:
        pass


class TransportDeleter:
    """:class:`AcquisitionDeleter` over a real :class:`CatalogClient`."""

    def __init__(self, client: CatalogClient) -> None:
        self._client = client

    def delete_acquisition(self, acquisition_id: UUID) -> None:
        delete_acquisition(self._client, acquisition_id)
