"""Every log line the app emits must be parseable JSON."""

from __future__ import annotations

import json
import logging
from pathlib import Path

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
        JsonFormatter().format(_record(key_owner="alice", tool="memory_get", duration_ms=12))
    )
    assert with_extra["key_owner"] == "alice"
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


def test_logs_go_to_stderr_never_stdout() -> None:
    """MCP reserves stdout for protocol traffic; diagnostics belong on stderr."""
    import sys

    from app.log import configure_logging

    configure_logging(env={})
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert handlers[0].stream is sys.stderr


def test_no_log_file_is_created_by_default(tmp_path) -> None:
    from app.log import configure_logging
    from app.paths import resolve_paths

    env = {"ARCHIVIST_HOME": str(tmp_path / "root")}
    configure_logging(env=env)
    assert not resolve_paths(env).logs.exists()
    assert len(logging.getLogger().handlers) == 1


def test_opt_in_log_file_lives_under_the_root_and_rotates(tmp_path) -> None:
    from logging.handlers import RotatingFileHandler

    from app.log import LOG_MAX_BYTES, configure_logging
    from app.paths import resolve_paths

    env = {"ARCHIVIST_HOME": str(tmp_path / "root"), "ARCHIVIST_LOG_FILE": "1"}
    paths = resolve_paths(env)
    try:
        configure_logging(env=env)
        file_handlers = [
            h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)
        ]
        assert len(file_handlers) == 1
        handler = file_handlers[0]
        assert Path(handler.baseFilename) == paths.log_file
        assert paths.log_file.is_relative_to(paths.data)
        # Bounded forever: one live file plus one backup, each capped.
        assert handler.maxBytes == LOG_MAX_BYTES
        assert handler.backupCount == 1

        logging.getLogger("archivist").info("hello")
        assert (
            json.loads(paths.log_file.read_text(encoding="utf-8").splitlines()[0])["msg"] == "hello"
        )
    finally:
        for h in list(logging.getLogger().handlers):
            h.close()
        configure_logging(env={})


def test_an_unwritable_root_does_not_stop_startup(tmp_path, monkeypatch) -> None:
    from app.log import configure_logging

    def boom(*_args, **_kwargs):
        raise OSError("EACCES")

    monkeypatch.setattr(Path, "mkdir", boom)
    configure_logging(env={"ARCHIVIST_HOME": str(tmp_path / "root"), "ARCHIVIST_LOG_FILE": "1"})
    assert len(logging.getLogger().handlers) == 1  # stderr only, no crash
