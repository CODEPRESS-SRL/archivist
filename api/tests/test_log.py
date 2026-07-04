"""Every log line the app emits must be parseable JSON (T0.4 DoD)."""

from __future__ import annotations

import json
import logging

from app.log import JsonFormatter


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="archivist",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_format_is_parseable_json_with_core_fields() -> None:
    line = JsonFormatter().format(_record())
    payload = json.loads(line)  # raises if not valid JSON

    assert payload["level"] == "INFO"
    assert payload["msg"] == "hello world"  # %-args interpolated
    assert "timestamp" in payload


def test_optional_fields_included_when_present_omitted_otherwise() -> None:
    with_extra = json.loads(
        JsonFormatter().format(_record(key_owner="enrico", tool="memory_get", duration_ms=12))
    )
    assert with_extra["key_owner"] == "enrico"
    assert with_extra["tool"] == "memory_get"
    assert with_extra["duration_ms"] == 12

    without_extra = json.loads(JsonFormatter().format(_record()))
    assert "key_owner" not in without_extra
    assert "tool" not in without_extra
    assert "duration_ms" not in without_extra


def test_exception_info_is_serialized() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()

    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc"]
