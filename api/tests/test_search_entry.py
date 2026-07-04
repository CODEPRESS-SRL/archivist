"""run_search dispatches to hybrid/lexical exactly like the MCP tool."""

from __future__ import annotations

import pytest

from app import db as app_db
from app.config import get_settings
from app.services import search_entry
from app.services.memory import create_entry
from app.services.search import search_memories


async def test_lexical_mode_matches_search_memories(
    db_engine, seed_team, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hybrid is the GA'd default (gap-closure S1) — force it off so this test
    # exercises the lexical branch: run_search must return exactly what
    # search_memories returns (the agent-facing lexical payload).
    get_settings.cache_clear()
    monkeypatch.setenv("ENABLE_HYBRID_SEARCH", "false")
    try:
        team_id, _ = await seed_team("acme")
        async with app_db.SessionFactory() as session:
            await create_entry(
                session, team_id, "billing-api", "redis-cache", "about caching", "agent"
            )
        async with app_db.SessionFactory() as session:
            via_entry = await search_entry.run_search(session, team_id, "billing-api", "caching")
        async with app_db.SessionFactory() as session:
            direct = await search_memories(session, team_id, "billing-api", "caching")
    finally:
        get_settings.cache_clear()

    assert via_entry == direct
    assert [r["keyword"] for r in via_entry["results"]] == ["redis-cache"]


async def test_dispatches_to_hybrid_when_enabled(
    db_engine, seed_team, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("ENABLE_HYBRID_SEARCH", "true")
    called = {}

    async def fake_hybrid(session, embedder, tid, project, query, limit, scope):
        called["hit"] = True
        return {"results": [], "related_keywords": [], "vector_rescued": False}

    monkeypatch.setattr(search_entry, "hybrid_search", fake_hybrid)

    try:
        team_id, _ = await seed_team("acme")
        async with app_db.SessionFactory() as session:
            await search_entry.run_search(
                session, team_id, "billing-api", "x", embedder_provider=lambda: object()
            )
    finally:
        get_settings.cache_clear()

    assert called.get("hit") is True
