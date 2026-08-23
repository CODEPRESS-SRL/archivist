"""Structured JSON logging to stdout (stdlib only, no extra libraries).

``configure_logging`` installs a single stdout handler on the root logger
that emits one JSON object per line. ``JsonFormatter`` always includes
``timestamp``/``level``/``msg`` and, when present on the record's ``extra``,
the operational fields ``key_owner``/``project``/``keyword``/``tool``/
``duration_ms``/``outcome``/``result_count``/``intent``/``search_mode``/
``vector_rescued`` — the per-tool audit line emitted by the MCP layer (T2.2;
``result_count`` added for the T3.4 pilot's zero-result search-rate metric;
``intent`` for the v1.x #1 read/write tag that de-confounds it; ``search_mode``
and ``vector_rescued`` for the v1.x #2 hybrid-retrieval flag switch).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

from app.paths import resolve_paths

# Ceiling for the opt-in log file; with backupCount=1 the on-disk cost of
# logging can never exceed twice this.
LOG_MAX_BYTES = 1024 * 1024

# Optional fields promoted from ``logger.info(..., extra={...})`` to the top
# level of the JSON line. Absent fields are simply omitted, keeping lines lean.
_EXTRA_FIELDS = (
    "key_owner",
    "project",
    "keyword",
    "tool",
    "duration_ms",
    "outcome",
    "result_count",
    "intent",
    "search_mode",
    "vector_rescued",
)


class JsonFormatter(logging.Formatter):
    """Render a ``LogRecord`` as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO, env: dict[str, str] | None = None) -> None:
    """Point the root logger at a JSON **stderr** handler (idempotent).

    stderr, not stdout: the MCP spec reserves stdout for protocol traffic, so a
    diagnostic line there corrupts the stream for any stdio-transport client.

    A log FILE is opt-in (``ARCHIVIST_LOG_FILE=1``), lives under the Archivist
    root, and is rotated at 1 MB keeping one backup — bounded at 2 MB forever.
    Nothing is written to disk by default.

    Single-process only: ``RotatingFileHandler`` rollover is not safe across
    processes, so do not enable it on the api and the embed worker at the same
    time (they resolve the same file). Under Docker prefer the container log
    driver — ``docker compose logs -f api`` — and leave this off.
    """
    env = os.environ if env is None else env
    handler: logging.Handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    if env.get("ARCHIVIST_LOG_FILE") not in ("1", "true"):
        return
    try:
        log_file = resolve_paths(env).log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8"
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)
    except OSError:
        # An unwritable root must never stop the process from starting.
        pass


def get_logger(name: str = "archivist") -> logging.Logger:
    """Return the application logger; children inherit the JSON stdout handler."""
    return logging.getLogger(name)
