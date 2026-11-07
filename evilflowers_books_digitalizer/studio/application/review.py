"""Operator notes and review decisions.

Approving a book rebuilds its outputs and publishes it (sequence
"approve and publish") unless the project never publishes on its own
(``auto_publish = "never"``): a RENDER run and a PUBLISH run are recorded in
that order and enqueued after the commit. Ordering them is the batch
orchestrator's job (``studio.run`` holds a publish behind an earlier active
render: ``workers.stages.orchestrate.blocked_by``). The render run also rewrites
the IA documents, so the operator's page edits reach ``_scandata.xml``.
"""

from __future__ import annotations

from evilflowers_books_digitalizer.studio.application import dto
from evilflowers_books_digitalizer.studio.application.errors import BadRequest
from evilflowers_books_digitalizer.studio.application.lookup import get_book, get_page, get_region
from evilflowers_books_digitalizer.studio.application.runs import (
    RunSpec,
    add_run,
    check_active,
    enqueue,
    set_status,
)
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.application.uow import Note, Review, Run, Session, UnitOfWork
from evilflowers_books_digitalizer.studio.domain import (
    BookStatus,
    PageStatus,
    ReviewDecision,
    RunKind,
)


# ---------------------------------------------------------------- notes
def add_note(svc: Services, cmd: dto.NoteIn, author: str | None) -> dto.NoteOut:
    """A note on a book, a page or a region; the most specific target decides the rest."""
    with svc.uow() as uow:
        book_id, page_id = _note_target(uow.session, cmd)
        note = Note(book_pk=book_id, page_pk=page_id, region_pk=cmd.region_id,
                    author=author, text=cmd.text.strip())
        uow.session.add(note)
        uow.session.flush()
        return dto.note_out(note)


def _note_target(session: Session, cmd: dto.NoteIn) -> tuple[int, int | None]:
    page_id = cmd.page_id
    if cmd.region_id is not None:
        region = get_region(session, cmd.region_id)
        if page_id is not None and page_id != region.page_pk:
            raise BadRequest(f"region {region.id} is not on page {page_id}")
        page_id = region.page_pk
    if page_id is not None:
        book_id = get_page(session, page_id).book_pk
        if cmd.book_id is not None and cmd.book_id != book_id:
            raise BadRequest(f"page {page_id} does not belong to book {cmd.book_id}")
        return book_id, page_id
    if cmd.book_id is None:
        raise BadRequest("a note needs book_id, page_id or region_id")
    return get_book(session, cmd.book_id).id, None


# ---------------------------------------------------------------- reviews
def submit_review(svc: Services, book_id: int, cmd: dto.ReviewIn, author: str | None
                  ) -> dto.ReviewOut:
    with svc.uow() as uow:
        book = get_book(uow.session, book_id)
        review = Review(book=book, decision=cmd.decision, author=author, note=cmd.note)
        uow.session.add(review)
        runs: list[Run] = []
        match cmd.decision:
            case ReviewDecision.APPROVED:
                set_status(uow, book, BookStatus.APPROVED)
                for page in book.pages:      # the operator signed off every leaf
                    page.status = PageStatus.DONE
                if _publishes_after_review(svc, book.project):
                    runs = _render_and_publish(uow, book_id, author)
            case ReviewDecision.NEEDS_WORK:
                set_status(uow, book, BookStatus.REVIEW)
            case ReviewDecision.REJECTED:
                pass   # recorded only: the book stays where it is, out of the publish path
        uow.session.flush()
        out = dto.review_out(review)
    out.runs = enqueue(svc, runs) if runs else []
    return out


def _publishes_after_review(svc: Services, project: str | None) -> bool:
    cfg = svc.project(project)
    return cfg is not None and cfg.auto_publish != "never"


def _render_and_publish(uow: UnitOfWork, book_id: int, author: str | None) -> list[Run]:
    book = get_book(uow.session, book_id)
    check_active(uow.session, book, RunKind.RENDER)
    return [add_run(uow, book, RunSpec(RunKind.RENDER), author),
            add_run(uow, book, RunSpec(RunKind.PUBLISH), author)]
