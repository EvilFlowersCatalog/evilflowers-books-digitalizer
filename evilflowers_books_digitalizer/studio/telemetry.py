"""OpenTelemetry for the digitalizer (API and workers), per the cluster's telemetry contract.

knowledge ``Cookbooks/Deploy-a-Service`` §3: the pod sets ``OTEL_SERVICE_NAME`` (= the
``app.kubernetes.io/name`` label), ``OTEL_EXPORTER_OTLP_ENDPOINT`` (the node's alloy-node,
``http://$(HOST_IP):4318``), ``OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`` and
``OTEL_RESOURCE_ATTRIBUTES`` (``service.namespace=elvira``, ``deployment.environment.name``,
k8s ids), plus the label ``elvira.stuba.sk/instrumentation: otel-sdk`` so Beyla does not
trace the pod twice.

Everything is a no-op without an endpoint or without the SDK (the ``telemetry`` group in
pyproject.toml), so tests and a laptop need nothing. With it:

* Celery: a CONSUMER span per task, PRODUCER spans per message; ``traceparent`` travels in
  the message headers, so one trace runs API -> orchestrator -> a service task (the kit's
  services continue it) -> the ``job_done`` callback;
* FastAPI (server spans, ``/healthz`` and ``/metrics`` excluded), SQLAlchemy, Redis, HTTP
  clients (the catalog publisher, the VLM);
* :func:`bind`: ledger ids on the current span *and* on every log line underneath, under
  the same names (``digitalizer.run.id`` <-> ``run_id``), so Loki and Tempo link up.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from evilflowers_books_digitalizer.logs import log_context

logger = logging.getLogger(__name__)

ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
#: log field -> span attribute (one spelling per id across logs and traces)
ATTRIBUTES = {
    "project": "digitalizer.project",
    "book_pk": "digitalizer.book.id",
    "run_id": "digitalizer.run.id",
    "job_id": "digitalizer.job.id",
    "stage": "digitalizer.stage",
    "lane": "digitalizer.lane",
    "task_id": "messaging.message.id",
}
_configured = False


def enabled() -> bool:
    return bool(os.environ.get(ENDPOINT_ENV))


def configure(default_service: str) -> bool:
    """Install the tracer provider and the library instrumentations once per process."""
    global _configured
    if _configured or not enabled():
        return _configured
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("%s is set but the OpenTelemetry SDK is not installed "
                       "(poetry install --with telemetry); tracing stays off", ENDPOINT_ENV)
        return False
    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", default_service),
        "service.namespace": "elvira",
        "service.version": os.environ.get("IMAGE_TAG", "dev"),
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _instrument_libraries()
    _configured = True
    logger.info("OpenTelemetry tracing on (%s)", resource.attributes.get("service.name"))
    return True


def _instrument_libraries() -> None:
    for module, cls in (("opentelemetry.instrumentation.celery", "CeleryInstrumentor"),
                        ("opentelemetry.instrumentation.sqlalchemy", "SQLAlchemyInstrumentor"),
                        ("opentelemetry.instrumentation.redis", "RedisInstrumentor"),
                        ("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
                        ("opentelemetry.instrumentation.requests", "RequestsInstrumentor")):
        try:
            instrumentor = getattr(__import__(module, fromlist=[cls]), cls)
        except (ImportError, AttributeError):
            continue
        try:
            instrumentor().instrument()
        except Exception as exc:  # noqa: BLE001 - telemetry never stops the service
            logger.warning("could not instrument %s: %s", module, exc)


def instrument_fastapi(app: Any) -> None:
    if not configure("digitalizer-api"):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        return
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,metrics")


def annotate(**fields: Any) -> None:
    """Ledger ids on the current span (no-op without OpenTelemetry or outside a span)."""
    try:
        from opentelemetry import trace
    except ImportError:
        return
    span = trace.get_current_span()
    if not span.is_recording():
        return
    for key, value in fields.items():
        if value is not None:
            span.set_attribute(ATTRIBUTES.get(key, f"digitalizer.{key}"), value)


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """:func:`annotate` + :func:`logs.log_context`: the same ids on the span and the log lines
    (the JSON formatter adds ``trace_id`` / ``span_id`` itself)."""
    annotate(**fields)
    with log_context(**fields):
        yield
