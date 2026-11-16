"""Orchestration: the job ledger, admission (scheduling) and the worker contract.

The digitalizer is the system of record for its runs. A run's stages are planned as
``jobs`` rows (:mod:`.ledger`); the scheduler's pump admits them by lane and time of day
(:mod:`.scheduling`) and sends each with Celery ``link`` / ``link_error`` callbacks
(:mod:`.transport`); results come back on ``digitalizer.job_done`` /
``digitalizer.job_failed`` and, for standalone services, as outcome files
(:mod:`.contract`). Diagrams: docs/diagrams/sequence-digitalizer-full-run,
state-digitalizer-run-job.
"""
