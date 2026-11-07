"""Batch side of the studio: Celery app (:mod:`.app`), tasks (:mod:`.tasks`), stage bodies
(:mod:`.stages`) and the adapter registry (:mod:`.registry`).

Nothing is imported here on purpose: the API process imports only
:mod:`studio.adapters.queue` (task names), never the worker stack.
"""
