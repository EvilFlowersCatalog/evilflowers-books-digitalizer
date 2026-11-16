"""JSON endpoints of /api/v1 (docs/design/studio-spec.md, "REST API").

Thin by design: parse, call one use case, return its DTO. Endpoints are plain
``def`` so FastAPI runs the (blocking, SQLAlchemy) use cases in its thread pool.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Body, Query, Response

from evilflowers_books_digitalizer.studio.api.deps import Operator, Svc
from evilflowers_books_digitalizer.studio.application.errors import BadRequest, Unavailable
from evilflowers_books_digitalizer.studio.application import (
    books,
    dto,
    labelling,
    marks,
    pages,
    regions,
    review,
    runs,
    scheduler,
    title_page,
)
from evilflowers_books_digitalizer.studio.labelling import STUDIO_KINDS
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


@router.get("/books/{book_id}/title-page", response_model=title_page.TitlePageOut)
def get_title_page(svc: Svc, book_id: int) -> title_page.TitlePageOut:
    """The decider's title / authors / publisher / year suggestions (#15), never imported."""
    return title_page.get(svc, book_id)


@router.post("/books/{book_id}/title-page/confirm", response_model=title_page.TitlePageOut)
def confirm_title_page(svc: Svc, who: Operator, book_id: int,
                       body: title_page.ConfirmIn) -> title_page.TitlePageOut:
    """The operator's final values become the metadata; the comparison a labelled case (#7)."""
    return title_page.confirm(svc, book_id, body, who)


# ---------------------------------------------------------------- scan marks (#14)
@router.get("/books/{book_id}/mark-groups", response_model=list[marks.MarkGroupOut])
def list_mark_groups(svc: Svc, book_id: int) -> list[marks.MarkGroupOut]:
    return marks.list_groups(svc, book_id)


@router.patch("/mark-groups/{group_id}", response_model=marks.MarkGroupOut)
def decide_mark_group(svc: Svc, who: Operator, group_id: int,
                      body: marks.MarkGroupPatch) -> marks.MarkGroupOut:
    return marks.decide(svc, group_id, body, who)


@router.post("/mark-groups/{group_id}/split", status_code=201, response_model=marks.MarkGroupOut)
def split_mark_group(svc: Svc, who: Operator, group_id: int,
                     body: marks.MarkGroupSplit) -> marks.MarkGroupOut:
    return marks.split(svc, group_id, body, who)


@router.get("/mark-groups/{group_id}/image")
def mark_group_image(svc: Svc, group_id: int) -> Response:
    return Response(marks.image(svc, group_id), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=300"})


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


# ---------------------------------------------------------------- scheduler
@router.get("/scheduler", response_model=dto.SchedulerOut)
def scheduler_status(svc: Svc) -> dto.SchedulerOut:
    return scheduler.status(svc)


@router.post("/scheduler/pause", response_model=dto.SchedulerOut)
def scheduler_pause(svc: Svc, who: Operator,
                    body: Annotated[dto.PauseIn | None, Body()] = None) -> dto.SchedulerOut:
    return scheduler.pause(svc, body or dto.PauseIn(), who)


@router.post("/scheduler/resume", response_model=dto.SchedulerOut)
def scheduler_resume(svc: Svc) -> dto.SchedulerOut:
    return scheduler.resume(svc)


# ---------------------------------------------------------------- labelling (#7)
# The same use cases serve the SPA (here), the CLI (`studio label`, `sample-labels`,
# `export-labels`) and MCP (studio/api/mcp.py, mounted at /mcp).
@router.post("/labelling/tasks/{task}/samples", status_code=202)
def labelling_sample(svc: Svc, task: str, background: BackgroundTasks,
                     n: Annotated[int, Query(ge=1, le=2000)] = 100) -> dict[str, Any]:
    """Add up to ``n`` items in the background (the catalog fan-out takes minutes); the task
    list's ``items`` count grows when it is done. Re-running only adds new items."""
    t = labelling.task_or_404(task)
    if not t.by_hand:
        raise BadRequest(f"{t.name} is not sampled: its cases come from confirmed metadata forms")
    if t.item_kind not in STUDIO_KINDS and not svc.config.labelling.enabled:
        raise Unavailable("labelling is not configured ([labelling] base_url)")
    background.add_task(labelling.sample_quietly, svc, task, n)
    return {"task": task, "n": n, "status": "sampling"}


@router.get("/labelling/tasks", response_model=list[labelling.TaskOut])
def labelling_tasks(svc: Svc, who: Operator) -> list[labelling.TaskOut]:
    return labelling.list_tasks(svc, who)


@router.get("/labelling/tasks/{task}/next", response_model=labelling.LabelItemOut | None)
def labelling_next(svc: Svc, who: Operator, task: str) -> labelling.LabelItemOut | None:
    """The next item for this labeller; ``null`` when none is left."""
    return labelling.next_item(svc, task, who)


@router.get("/labelling/tasks/{task}/summary", response_model=labelling.SummaryOut)
def labelling_summary(svc: Svc, task: str) -> labelling.SummaryOut:
    return labelling.summary(svc, task)


@router.get("/labelling/tasks/{task}/export")
def labelling_export(svc: Svc, task: str) -> Response:
    """JSONL, one line per non-skip label (the calibration input of knowledge#65)."""
    body = "".join(json.dumps(line, ensure_ascii=False) + "\n"
                   for line in labelling.export_lines(svc, task))
    return Response(body, media_type="application/x-ndjson",
                    headers={"Content-Disposition": f'attachment; filename="labels-{task}.jsonl"'})


@router.get("/labelling/items/{item_id}", response_model=labelling.LabelItemOut)
def labelling_item(svc: Svc, who: Operator, item_id: int) -> labelling.LabelItemOut:
    return labelling.get_item(svc, item_id, who)


@router.post("/labelling/items/{item_id}/labels", response_model=labelling.LabelItemOut)
def labelling_submit(svc: Svc, who: Operator, item_id: int,
                     body: labelling.LabelsIn) -> labelling.LabelItemOut:
    return labelling.submit(svc, item_id, body, who)


@router.get("/labelling/items/{item_id}/image")
def labelling_image(svc: Svc, item_id: int) -> Response:
    """The region crop, proxied: the catalog key never reaches the browser."""
    img = labelling.item_image(svc, item_id)
    return Response(img.content, media_type=img.media_type,
                    headers={"Cache-Control": "private, max-age=86400"})
