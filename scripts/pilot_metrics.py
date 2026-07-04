#!/usr/bin/env python3
"""T3.4 pilot metrics collector.

Reads pilot signal OBJECTIVELY from two sources — never from agent self-report:

  1. The T2.2 JSON audit log (``docker compose logs api``): one ``tool_call``
     line per MCP call with ``tool``, ``key_owner``, ``outcome`` and (T3.4)
     ``result_count`` on searches.
  2. The database (``psql``): entries, fragments, history, sizes.

It computes the T3.4 metrics: per-tool call counts, active members,
entry / append / compaction counts, average sizes, 429 (RATE_LIMITED) / 409
(VERSION_CONFLICT) counts, potential duplicate keywords, the raw
**zero-result search rate**, and — the actual gating metric for the v1.x hybrid
BM25 + pgvector upgrade — the **corrected read-path zero-result rate**, which
excludes the write-path dedup probes that confound the raw rate (v1.x #1;
requires the ``intent`` audit tag on ``memory_search``).

Usage:
    python scripts/pilot_metrics.py --since 2026-07-02T20:00:00 \
        --owners alice,bob [--compose-file docker-compose.yml]

Env overrides (prod defaults): COMPOSE_FILE, POSTGRES_USER, POSTGRES_DB.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import Counter
from datetime import datetime

COMPOSE_FILE = os.environ.get("COMPOSE_FILE", "docker-compose.yml")
PG_USER = os.environ.get("POSTGRES_USER", "archivist")
PG_DB = os.environ.get("POSTGRES_DB", "archivist")


def _compose(*args: str) -> list[str]:
    return ["docker", "compose", "-f", COMPOSE_FILE, *args]


def _psql(query: str) -> str:
    """Run a query, return raw tab-separated rows (``-tA``)."""
    out = subprocess.run(
        _compose("exec", "-T", "db", "psql", "-U", PG_USER, "-d", PG_DB, "-tAc", query),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _audit_lines(since: datetime | None, owners: set[str] | None) -> list[dict]:
    """Parse tool_call audit lines, filtered by timestamp and key_owner."""
    raw = subprocess.run(
        _compose("logs", "api"), capture_output=True, text=True, check=True
    ).stdout
    lines: list[dict] = []
    for line in raw.splitlines():
        brace = line.find("{")
        if brace == -1:
            continue
        try:
            rec = json.loads(line[brace:])
        except json.JSONDecodeError:
            continue
        if rec.get("msg") != "tool_call":
            continue
        if since is not None:
            ts = datetime.fromisoformat(rec["timestamp"])
            if ts < since.replace(tzinfo=ts.tzinfo):
                continue
        if owners is not None and rec.get("key_owner") not in owners:
            continue
        lines.append(rec)
    return lines


def _audit_metrics(lines: list[dict]) -> dict:
    """Audit-derived metrics from parsed ``tool_call`` lines (no DB, no docker).

    Reports the raw ``zero_result_rate`` (all searches with a count) AND the
    **corrected** ``read_path_zero_result_rate`` — restricted to searches tagged
    ``intent="read"``, so the write-path dedup probes that mandatorily precede
    every create (and correctly return nothing on a cold namespace) no longer
    confound it (v1.x #1). Un-tagged / ``unspecified`` searches are excluded from
    the corrected rate, so a forgotten tag never re-pollutes the read bucket.
    This corrected rate is the trigger metric that gates the pgvector upgrade.
    """
    by_tool = Counter(rec["tool"] for rec in lines)
    by_outcome = Counter(rec["outcome"] for rec in lines)
    active_members = sorted({rec["key_owner"] for rec in lines})
    searches = [rec for rec in lines if rec["tool"] == "memory_search"]
    with_count = [rec for rec in searches if "result_count" in rec]
    zero = [rec for rec in with_count if rec["result_count"] == 0]
    zero_rate = (len(zero) / len(with_count)) if with_count else None

    by_intent = Counter(rec.get("intent", "unspecified") for rec in searches)
    read_with_count = [rec for rec in with_count if rec.get("intent") == "read"]
    read_zero = [rec for rec in read_with_count if rec["result_count"] == 0]
    read_rate = (len(read_zero) / len(read_with_count)) if read_with_count else None

    # Per-mode read-path zero rate (H8): the A/B signal for lexical vs hybrid.
    by_mode: dict[str, float] = {}
    for mode in {
        rec.get("search_mode") for rec in read_with_count if rec.get("search_mode")
    }:
        bucket = [rec for rec in read_with_count if rec.get("search_mode") == mode]
        zeros = [rec for rec in bucket if rec["result_count"] == 0]
        by_mode[mode] = (len(zeros) / len(bucket)) if bucket else None
    vector_rescued_reads = sum(
        1 for rec in read_with_count if rec.get("vector_rescued") is True
    )

    return {
        "total_calls": len(lines),
        "by_tool": dict(by_tool),
        "by_outcome": dict(by_outcome),
        "active_members": active_members,
        "searches": len(searches),
        "searches_with_count": len(with_count),
        "zero_result_searches": len(zero),
        "zero_result_rate": zero_rate,
        "searches_by_intent": dict(by_intent),
        "read_searches_with_count": len(read_with_count),
        "read_zero_result_searches": len(read_zero),
        "read_path_zero_result_rate": read_rate,
        "read_path_zero_result_rate_by_mode": by_mode,
        "vector_rescued_reads": vector_rescued_reads,
        "rate_limited_429": by_outcome.get("RATE_LIMITED", 0),
        "version_conflict_409": by_outcome.get("VERSION_CONFLICT", 0),
    }


def collect(since: datetime | None, owners: set[str] | None) -> dict:
    lines = _audit_lines(since, owners)
    owner_filter = (
        "(" + ",".join(f"'{o}'" for o in owners) + ")" if owners else None
    )

    # --- DB-derived metrics (optionally scoped to the pilot members) -------
    where_member = f"AND updated_by IN {owner_filter}" if owner_filter else ""
    entries_total = int(_psql("SELECT count(*) FROM memories WHERE deleted_at IS NULL;"))
    entries_by_project = _psql(
        "SELECT p.slug, count(*) FROM memories m JOIN projects p ON p.id=m.project_id "
        "WHERE m.deleted_at IS NULL GROUP BY p.slug ORDER BY p.slug;"
    )
    entries_by_member = _psql(
        "SELECT updated_by, count(*) FROM memories WHERE deleted_at IS NULL "
        "GROUP BY updated_by ORDER BY updated_by;"
    )
    fragments = int(_psql("SELECT count(*) FROM memory_fragments;"))
    # A compaction writes a history row at version >= 2; a create writes v1.
    compactions = int(
        _psql(f"SELECT count(*) FROM memory_history WHERE version >= 2 {where_member};")
    )
    avg_entry = _psql("SELECT COALESCE(round(avg(token_count)),0) FROM memories WHERE deleted_at IS NULL;")
    avg_fragment = _psql("SELECT COALESCE(round(avg(token_count)),0) FROM memory_fragments;")
    # Potential duplicate keywords: high trigram similarity within a project.
    dup_pairs = _psql(
        "SELECT a.slug, m1.keyword, m2.keyword, round(similarity(m1.keyword,m2.keyword)::numeric,2) "
        "FROM memories m1 JOIN memories m2 ON m1.project_id=m2.project_id AND m1.id<m2.id "
        "JOIN projects a ON a.id=m1.project_id "
        "WHERE m1.deleted_at IS NULL AND m2.deleted_at IS NULL "
        "AND similarity(m1.keyword,m2.keyword)>0.4 ORDER BY 4 DESC;"
    )

    return {
        "audit": _audit_metrics(lines),
        "db": {
            "entries_total": entries_total,
            "entries_by_project": entries_by_project,
            "entries_by_member": entries_by_member,
            "fragments_pending": fragments,
            "compactions": compactions,
            "avg_entry_tokens": avg_entry,
            "avg_fragment_tokens": avg_fragment,
            "potential_duplicate_keyword_pairs": dup_pairs or "(none > 0.4 similarity)",
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help="ISO timestamp; only audit lines at/after it")
    ap.add_argument("--owners", help="comma-separated key_owner filter (audit + DB member scope)")
    args = ap.parse_args()

    since = datetime.fromisoformat(args.since) if args.since else None
    owners = set(args.owners.split(",")) if args.owners else None

    report = collect(since, owners)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
