"""Application errors: what a use case can refuse, independent of HTTP.

The API maps them to status codes (404 / 409 / 400 / 422); the CLI prints them.
Keeping the mapping in the shell lets the same use cases serve both.
"""

from __future__ import annotations


class ApplicationError(Exception):
    """Base class: an expected refusal with an operator-readable message."""


class NotFound(ApplicationError):
    """The addressed book / page / run / region does not exist (404)."""


class Conflict(ApplicationError):
    """Valid request, but the current state forbids it, e.g. a run is already active (409)."""


class BadRequest(ApplicationError):
    """The request is incomplete or contradicts itself, e.g. a note with no target (400)."""


class Invalid(ApplicationError):
    """A value fails a domain rule: bad page index, malformed IA date, empty bbox (422)."""


class Unavailable(ApplicationError):
    """A required backend (the task broker) refused the work; nothing was scheduled (503)."""
