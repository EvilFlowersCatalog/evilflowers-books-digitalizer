"""Telemetry: off by default; with a tracer, ledger ids land on the span and the log lines."""

from __future__ import annotations

import io
import json
import logging

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
