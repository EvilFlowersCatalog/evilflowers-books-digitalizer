"""Use cases of the restoration studio (interactive side): books, runs, pages, regions, review.

Shells (``studio.api``, the ``studio`` CLI group) build a :class:`~.services.Services`
and call the functions in the submodules; every function opens its own
:class:`~.uow.UnitOfWork` and returns pydantic DTOs (:mod:`.dto`).
"""
