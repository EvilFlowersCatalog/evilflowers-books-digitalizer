"""F: JSON log lines — shape, WARN label, context fields, exceptions."""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime

import pytest

from evilflowers_books_digitalizer import logs
from evilflowers_books_digitalizer.logs import log_context, setup_logging
from evilflowers_books_digitalizer.pipeline.base import BookContext, Pipeline, PipelineStep


def _json_lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


@pytest.fixture
def json_stream(monkeypatch) -> io.StringIO:
    monkeypatch.setenv(logs.FORMAT_ENV, "json")
    stream = io.StringIO()
    setup_logging(stream=stream)
    return stream


def test_json_line_shape_and_warn_label(json_stream):
    logging.getLogger("evilflowers_books_digitalizer.x").warning("disk %s", "low")
    (line,) = _json_lines(json_stream)
    assert set(line) == {"time", "level", "logger", "msg"}
    assert line["level"] == "WARN"
    assert line["logger"] == "evilflowers_books_digitalizer.x"
    assert line["msg"] == "disk low"
    stamp = datetime.fromisoformat(line["time"])
    assert line["time"].endswith("Z") and stamp.utcoffset().total_seconds() == 0


def test_context_fields_from_log_context_and_extra(json_stream):
    log = logging.getLogger("t")
    with log_context(source="svf", book_id="B1", slug="svf_B1"):
        with log_context(step="ocr"):
            log.info("inner", extra={"duration_ms": 12, "status": "ok"})
        log.info("outer", extra={"entry_id": "e-1"})
    log.info("after")
    inner, outer, after = _json_lines(json_stream)
    expected = {"source": "svf", "book_id": "B1", "slug": "svf_B1", "step": "ocr",
                "duration_ms": 12, "status": "ok", "msg": "inner", "level": "INFO"}
    assert {key: inner.get(key) for key in expected} == expected
    assert "step" not in outer and outer["entry_id"] == "e-1" and outer["book_id"] == "B1"
    assert not {"source", "book_id", "step"} & set(after)


def test_exceptions_carry_type_and_traceback(json_stream):
    try:
        raise RuntimeError("tesseract died")
    except RuntimeError:
        logging.getLogger("t").error("step failed", exc_info=True)
    (line,) = _json_lines(json_stream)
    assert line["level"] == "ERROR"
    assert line["exc_type"] == "RuntimeError"
    assert "tesseract died" in line["exc"] and "Traceback" in line["exc"]


def test_pipeline_steps_set_the_step_field(json_stream, tmp_path):
    class Probe(PipelineStep):
        name = "probe"

        def run(self, ctx: BookContext) -> BookContext:
            logging.getLogger("probe").info("working")
            return ctx

    ctx = BookContext(source="svf", book_id="B1", work_dir=tmp_path, output_dir=tmp_path)
    with log_context(source="svf", book_id="B1"):
        Pipeline([Probe()]).run(ctx)
    lines = _json_lines(json_stream)
    working = next(line for line in lines if line["msg"] == "working")
    assert working["step"] == "probe" and working["book_id"] == "B1"
    done = next(line for line in lines if line["msg"].startswith("step probe"))
    assert done["step"] == "probe" and isinstance(done["duration_ms"], int)


def test_level_env_and_warn_alias(monkeypatch):
    monkeypatch.setenv(logs.FORMAT_ENV, "json")
    monkeypatch.setenv(logs.LEVEL_ENV, "WARN")
    stream = io.StringIO()
    setup_logging(stream=stream)
    logging.getLogger("t").info("hidden")
    logging.getLogger("t").warning("shown")
    assert [line["msg"] for line in _json_lines(stream)] == ["shown"]


def test_text_is_the_default_and_shows_context(monkeypatch):
    stream = io.StringIO()
    setup_logging(stream=stream)
    with log_context(source="svf", book_id="B1", step="ocr"):
        logging.getLogger("t").info("hello")
    text = stream.getvalue()
    assert "INFO t [svf/B1 step=ocr]: hello" in text
    assert not logs.is_json()


def test_bad_env_values_are_rejected(monkeypatch):
    monkeypatch.setenv(logs.FORMAT_ENV, "xml")
    with pytest.raises(ValueError):
        setup_logging(stream=io.StringIO())
    monkeypatch.setenv(logs.FORMAT_ENV, "json")
    monkeypatch.setenv(logs.LEVEL_ENV, "LOUD")
    with pytest.raises(ValueError):
        setup_logging(stream=io.StringIO())


def test_setup_is_idempotent_and_keeps_the_file_handler(monkeypatch, tmp_path):
    monkeypatch.setenv(logs.FORMAT_ENV, "json")
    stream = io.StringIO()
    setup_logging(stream=stream)
    handler = logs.add_file_handler(tmp_path / "logs" / "digitizer.log")
    try:
        setup_logging(stream=io.StringIO())  # e.g. process_book in-process: no-op
        logging.getLogger("t").warning("both")
        assert _json_lines(stream)[0]["msg"] == "both"
        file_line = json.loads((tmp_path / "logs" / "digitizer.log").read_text().splitlines()[0])
        assert file_line["msg"] == "both" and file_line["level"] == "WARN"
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
