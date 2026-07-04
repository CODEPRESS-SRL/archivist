"""Cross-team isolation at the service layer (Task 3) and tool layer (Task 5)."""
from __future__ import annotations

import pytest

from app import db as app_db
from app.auth import AuthContext, create_team, current_key
from app.mcp_server import build_mcp
from app.services import memory as memory_svc
from app.services import search as search_svc


@pytest.fixture
async def two_teams(db_engine: object) -> tuple[int, int]:
    async with app_db.SessionFactory() as session:
        a = await create_team(session, "team-a")
        b = await create_team(session, "team-b")
    return a, b


async def test_get_does_not_cross_teams(two_teams: tuple[int, int]) -> None:
    team_a, team_b = two_teams
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_a, "billing-api", "redis-cache", "A secret", "team-a")

    # team B, same project slug + keyword → sees nothing.
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_b, "billing-api", ["redis-cache"])
    assert got["entries"] == []
    assert got["not_found"] == ["redis-cache"]


async def test_same_slug_two_teams_are_separate_projects(two_teams: tuple[int, int]) -> None:
    team_a, team_b = two_teams
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_a, "billing-api", "kw", "A content", "team-a")
    async with app_db.SessionFactory() as session:
        # No ALREADY_EXISTS: it is a different team's project.
        res = await memory_svc.create_entry(session, team_b, "billing-api", "kw", "B content", "team-b")
    assert res["created"] is True

    async with app_db.SessionFactory() as session:
        a = await memory_svc.get_entries(session, team_a, "billing-api", ["kw"])
        b = await memory_svc.get_entries(session, team_b, "billing-api", ["kw"])
    assert a["entries"][0]["content"] == "A content"
    assert b["entries"][0]["content"] == "B content"


async def test_append_delete_across_teams_is_not_found(two_teams: tuple[int, int]) -> None:
    team_a, team_b = two_teams
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_a, "billing-api", "kw", "A content", "team-a")
    async with app_db.SessionFactory() as session:
        with pytest.raises(memory_svc.ServiceError) as ei:
            await memory_svc.append_fragment(session, team_b, "billing-api", "kw", "frag", "team-b")
        assert ei.value.code == "NOT_FOUND"
    async with app_db.SessionFactory() as session:
        with pytest.raises(memory_svc.ServiceError) as ei:
            await memory_svc.delete_entry(session, team_b, "billing-api", "kw", "team-b")
        assert ei.value.code == "NOT_FOUND"


async def test_search_all_scope_stays_within_team(two_teams: tuple[int, int]) -> None:
    team_a, team_b = two_teams
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_a, "proj-a", "redis-cache", "team A note about redis", "team-a")

    # team B searches its own archive across ALL its projects → finds nothing.
    async with app_db.SessionFactory() as session:
        res = await search_svc.search_memories(session, team_b, "proj-a", "redis", scope="all")
    assert res["results"] == []
    assert res["related_keywords"] == []


# --- tool layer isolation (Task 5) ------------------------------------------


def _tool(mcp, name):
    # FastMCP stores registered callables; fetch by name for a direct call.
    return mcp._tool_manager._tools[name].fn


async def test_tools_isolate_by_team(two_teams: tuple[int, int]) -> None:
    team_a, team_b = two_teams
    mcp = build_mcp()
    upsert = _tool(mcp, "memory_upsert")
    get = _tool(mcp, "memory_get")

    tok = current_key.set(AuthContext(key_id=1, owner_name="ka", team_id=team_a, team_name="team-a"))
    try:
        await upsert("billing-api", "redis-cache", "A secret")
    finally:
        current_key.reset(tok)

    tok = current_key.set(AuthContext(key_id=2, owner_name="kb", team_id=team_b, team_name="team-b"))
    try:
        got = await get("billing-api", ["redis-cache"])
    finally:
        current_key.reset(tok)
    assert got["not_found"] == ["redis-cache"]
    assert got["entries"] == []
