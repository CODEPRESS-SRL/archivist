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
import sys
from datetime import UTC, datetime

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


def configure_logging(level: int = logging.INFO) -> None:
    """Point the root logger at a single JSON stdout handler (idempotent)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str = "archivist") -> logging.Logger:
    """Return the application logger; children inherit the JSON stdout handler."""
    return logging.getLogger(name)
