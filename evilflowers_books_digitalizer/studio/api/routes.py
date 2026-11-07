"""JSON endpoints of /api/v1 (docs/design/studio-spec.md, "REST API").

Thin by design: parse, call one use case, return its DTO. Endpoints are plain
``def`` so FastAPI runs the (blocking, SQLAlchemy) use cases in its thread pool.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Response

from evilflowers_books_digitalizer.studio.api.deps import Operator, Svc
from evilflowers_books_digitalizer.studio.application import (
    books,
    dto,
    pages,
    regions,
    review,
    runs,
)
from evilflowers_books_digitalizer.studio.domain import BookStatus

router = APIRouter()


# ---------------------------------------------------------------- projects & books
@router.get("/projects", response_model=list[dto.ProjectOut])
def list_projects(svc: Svc) -> list[dto.ProjectOut]:
    return books.list_projects(svc)


@router.get("/projects/{project}/config", response_model=dto.ProjectConfigOut)
def project_config(svc: Svc, project: str) -> dto.ProjectConfigOut:
    return books.project_config(svc, project)


@router.post("/projects/{project}/imports", status_code=202, response_model=dto.TaskOut)
def import_collection(svc: Svc, project: str,
                      body: Annotated[dto.ImportIn | None, Body()] = None) -> dto.TaskOut:
    return books.import_collection(svc, project, body.collection if body else None)


@router.get("/books", response_model=dto.BookPage)
def list_books(svc: Svc, project: str | None = None, collection: str | None = None,
               status: BookStatus | None = None, q: str | None = None,
               flagged: bool | None = None,
               limit: Annotated[int, Query(ge=1, le=500)] = 50,
               offset: Annotated[int, Query(ge=0)] = 0, sort: str = "id") -> dto.BookPage:
    query = dto.BookQuery(project=project, collection=collection, status=status, q=q,
                          flagged=flagged, limit=limit, offset=offset, sort=sort)
    return books.list_books(svc, query)


@router.get("/books/{book_id}", response_model=dto.BookDetail)
def get_book(svc: Svc, book_id: int) -> dto.BookDetail:
    return books.get_book(svc, book_id)


@router.patch("/books/{book_id}/metadata")
def update_metadata(svc: Svc, book_id: int, patch: Annotated[dict[str, Any], Body()]
                    ) -> dict[str, Any]:
    return books.update_item_metadata(svc, book_id, patch)


# ---------------------------------------------------------------- runs
@router.post("/books/{book_id}/runs", status_code=202, response_model=dto.RunOut)
def create_run(svc: Svc, who: Operator, book_id: int, body: dto.RunIn) -> dto.RunOut:
    return runs.create_run(svc, book_id, body, who)


@router.get("/runs/{run_id}", response_model=dto.RunOut)
def get_run(svc: Svc, run_id: int) -> dto.RunOut:
    return runs.read_run(svc, run_id)


@router.post("/runs/{run_id}/cancel", response_model=dto.RunOut)
def cancel_run(svc: Svc, who: Operator, run_id: int) -> dto.RunOut:
    return runs.cancel_run(svc, run_id, who)


# ---------------------------------------------------------------- pages & regions
@router.get("/books/{book_id}/pages", response_model=list[dto.PageOut])
def list_pages(svc: Svc, book_id: int) -> list[dto.PageOut]:
    return pages.list_pages(svc, book_id)


@router.get("/pages/{page_id}", response_model=dto.PageDetail)
def get_page(svc: Svc, page_id: int) -> dto.PageDetail:
    return pages.get_page_detail(svc, page_id)


@router.patch("/pages/{page_id}", response_model=dto.PageDetail)
def update_page(svc: Svc, page_id: int, body: dto.PagePatch) -> dto.PageDetail:
    return pages.update_page(svc, page_id, body)


@router.post("/pages/{page_id}/regions", status_code=201, response_model=dto.RegionOut)
def create_region(svc: Svc, who: Operator, page_id: int, body: dto.RegionIn) -> dto.RegionOut:
    return regions.create_region(svc, page_id, body, who)


@router.patch("/regions/{region_id}", response_model=dto.RegionOut)
def update_region(svc: Svc, who: Operator, region_id: int, body: dto.RegionPatch) -> dto.RegionOut:
    return regions.update_region(svc, region_id, body, who)


@router.delete("/regions/{region_id}", status_code=204)
def delete_region(svc: Svc, who: Operator, region_id: int) -> Response:
    regions.reject_region(svc, region_id, who)
    return Response(status_code=204)


# ---------------------------------------------------------------- notes & reviews
@router.post("/notes", status_code=201, response_model=dto.NoteOut)
def add_note(svc: Svc, who: Operator, body: dto.NoteIn) -> dto.NoteOut:
    return review.add_note(svc, body, who)


@router.post("/books/{book_id}/reviews", status_code=201, response_model=dto.ReviewOut)
def submit_review(svc: Svc, who: Operator, book_id: int, body: dto.ReviewIn) -> dto.ReviewOut:
    return review.submit_review(svc, book_id, body, who)
