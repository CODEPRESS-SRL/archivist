"""Unit test for the pilot-metrics audit bucketing.

Covers the v1.x #1 change: ``scripts/pilot_metrics.py`` reads the ``intent`` tag
on ``memory_search`` audit lines and reports the **corrected read-path
zero-result rate** — write-path dedup probes excluded — alongside the raw rate.

The audit-derived metrics live in the pure ``_audit_metrics(lines)`` helper so
this test needs neither docker nor a database; it feeds synthetic audit lines.
The collector is a repo-root script, imported here by path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PILOT = Path(__file__).resolve().parents[2] / "scripts" / "pilot_metrics.py"
_spec = importlib.util.spec_from_file_location("pilot_metrics", _PILOT)
assert _spec is not None and _spec.loader is not None
pilot_metrics = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pilot_metrics)


def _search(intent: str, count: int) -> dict:
    return {
        "msg": "tool_call",
        "tool": "memory_search",
        "key_owner": "a",
        "outcome": "ok",
        "result_count": count,
        "intent": intent,
    }


def test_audit_metrics_reports_corrected_read_path_zero_rate() -> None:
    lines = [
        _search("write", 0),  # dedup probe, correctly empty — excluded from read rate
        _search("write", 0),
        _search("read", 0),  # genuine read miss
        _search("read", 3),  # genuine read hit
        {"msg": "tool_call", "tool": "memory_upsert", "key_owner": "a", "outcome": "ok"},
    ]

    m = pilot_metrics._audit_metrics(lines)

    # Raw rate counts every search with a count: 3 of 4 are empty.
    assert m["searches_with_count"] == 4
    assert m["zero_result_searches"] == 3
    # Corrected read-path rate ignores write probes: 1 of 2 read searches empty.
    assert m["read_searches_with_count"] == 2
    assert m["read_zero_result_searches"] == 1
    assert m["read_path_zero_result_rate"] == 0.5
    assert m["searches_by_intent"] == {"read": 2, "write": 2}


def test_audit_metrics_splits_read_rate_by_mode():
    lines = [
        {
            "tool": "memory_search",
            "key_owner": "a",
            "outcome": "ok",
            "intent": "read",
            "result_count": 0,
            "search_mode": "lexical",
        },
        {
            "tool": "memory_search",
            "key_owner": "a",
            "outcome": "ok",
            "intent": "read",
            "result_count": 2,
            "search_mode": "hybrid",
            "vector_rescued": True,
        },
        {
            "tool": "memory_search",
            "key_owner": "a",
            "outcome": "ok",
            "intent": "read",
            "result_count": 0,
            "search_mode": "hybrid",
            "vector_rescued": False,
        },
    ]
    m = pilot_metrics._audit_metrics(lines)
    assert m["read_path_zero_result_rate_by_mode"]["lexical"] == 1.0
    assert m["read_path_zero_result_rate_by_mode"]["hybrid"] == 0.5
    assert m["vector_rescued_reads"] == 1
