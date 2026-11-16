"""Telemetry: off by default; with a tracer, ledger ids land on the span and the log lines."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from evilflowers_books_digitalizer import logs
from evilflowers_books_digitalizer.studio import telemetry


def test_off_without_an_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(telemetry.ENDPOINT_ENV, raising=False)
    assert telemetry.configure("digitalizer-test") is False
    with telemetry.bind(run_id=1, job_id=2):          # no span: still a valid context
        assert logs.current_context() == {"run_id": 1, "job_id": 2}


def test_bind_sets_span_attributes_and_log_lines_carry_the_trace() -> None:
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    stream = io.StringIO()
    logs.reset_logging()
    logs.setup_logging("json", "INFO", stream=stream, force=True)
    try:
        with tracer.start_as_current_span("digitalizer.job_done") as span:
            with telemetry.bind(run_id=7, job_id=11, stage="clean"):
                logging.getLogger("t").info("settled")
            ctx = span.get_span_context()
    finally:
        logs.reset_logging()
    (finished,) = exporter.get_finished_spans()
    assert finished.attributes == {"digitalizer.run.id": 7, "digitalizer.job.id": 11,
                                   "digitalizer.stage": "clean"}
    line = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert (line["run_id"], line["job_id"], line["stage"]) == (7, 11, "clean")
    assert line["trace_id"] == f"{ctx.trace_id:032x}" and line["span_id"] == f"{ctx.span_id:016x}"


def test_queries_through_the_studio_engine_are_traced(tmp_path, monkeypatch) -> None:
    """#13: the installed SQLAlchemy is one the instrumentation accepts, and both engines get
    SQL spans: the API's (``db.store.get_engine``, imported early) and a worker's
    (``workers.context.from_config``, what a ``clean`` job uses)."""
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    instr = pytest.importorskip("opentelemetry.instrumentation.sqlalchemy")
    from opentelemetry.instrumentation.dependencies import get_dependency_conflicts
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from sqlalchemy import text

    from evilflowers_books_digitalizer.db.store import get_engine
    from evilflowers_books_digitalizer.studio.config import load_config
    from evilflowers_books_digitalizer.studio.workers import context

    instrumentor = instr.SQLAlchemyInstrumentor()
    assert get_dependency_conflicts(instrumentor.instrumentation_dependencies()) is None
    exporter = InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setenv("STUDIO_DATABASE_URL", f"sqlite:///{tmp_path / 'worker.db'}")
    config = load_config(Path(__file__).parents[2] / "configs" / "studio.dev.toml")
    instrumentor.instrument(tracer_provider=provider)
    try:
        with get_engine(f"sqlite:///{tmp_path / 'traced.db'}").connect() as conn:
            conn.execute(text("SELECT 42"))
        session = context.from_config(config).session_factory()
        try:
            session.execute(text("SELECT 43"))
        finally:
            session.close()
    finally:
        instrumentor.uninstrument()
    statements = [s.attributes.get("db.statement") for s in exporter.get_finished_spans()]
    assert "SELECT 42" in statements and "SELECT 43" in statements


def test_celery_workers_log_through_the_app_handler() -> None:
    """#13: Celery neither installs its own handler nor proxies stdout/stderr at WARNING."""
    from celery.signals import setup_logging

    from evilflowers_books_digitalizer.studio.workers import app

    assert app.celery.conf.worker_redirect_stdouts is False
    assert app.celery.conf.worker_hijack_root_logger is False
    assert any(ref() is app._logging for _, ref in setup_logging.receivers)


def test_pil_debug_is_quiet() -> None:
    stream = io.StringIO()
    logs.reset_logging()
    logs.setup_logging("json", "DEBUG", stream=stream, force=True)
    try:
        logging.getLogger("PIL.Image").debug("failed to import MicImagePlugin")
        logging.getLogger("evilflowers").debug("kept")
    finally:
        logs.reset_logging()
    assert [json.loads(x)["msg"] for x in stream.getvalue().splitlines()] == ["kept"]
