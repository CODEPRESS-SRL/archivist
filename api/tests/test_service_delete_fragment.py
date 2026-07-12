"""Service-level delete_fragment: hard delete, scoping, tenancy."""

from __future__ import annotations

import pytest

from app import db as app_db
from app.services import memory as memory_svc
from app.services.memory import ServiceError


async def _seed_entry_with_two_fragments(team_id):
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_id, "billing-api", "redis-cache", "base", "agent")
        a = await memory_svc.append_fragment(
            session, team_id, "billing-api", "redis-cache", "frag-A", "agent"
        )
        b = await memory_svc.append_fragment(
            session, team_id, "billing-api", "redis-cache", "frag-B", "agent"
        )
    return a["fragment_id"], b["fragment_id"]


async def test_delete_fragment_removes_only_target(db_engine, seed_team) -> None:
    team_id, _ = await seed_team("acme")
    frag_a, frag_b = await _seed_entry_with_two_fragments(team_id)
    async with app_db.SessionFactory() as session:
        out = await memory_svc.delete_fragment(
            session, team_id, "billing-api", "redis-cache", frag_a
        )
    assert out == {"keyword": "redis-cache", "fragment_id": frag_a, "deleted": True}
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_id, "billing-api", ["redis-cache"])
    ids = [f["id"] for f in got["entries"][0]["fragments"]]
    assert ids == [frag_b]


async def test_delete_fragment_missing_fragment_raises_not_found(db_engine, seed_team) -> None:
    team_id, _ = await seed_team("acme")
    await _seed_entry_with_two_fragments(team_id)
    async with app_db.SessionFactory() as session:
        with pytest.raises(ServiceError) as exc:
            await memory_svc.delete_fragment(
                session, team_id, "billing-api", "redis-cache", 999999
            )
    assert exc.value.code == "NOT_FOUND"


async def test_delete_fragment_missing_entry_raises_not_found(db_engine, seed_team) -> None:
    team_id, _ = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        with pytest.raises(ServiceError) as exc:
            await memory_svc.delete_fragment(session, team_id, "billing-api", "ghost", 1)
    assert exc.value.code == "NOT_FOUND"


async def test_delete_fragment_is_tenant_isolated(db_engine, seed_team) -> None:
    team_a, _ = await seed_team("a")
    team_b, _ = await seed_team("b")
    frag_a, _ = await _seed_entry_with_two_fragments(team_a)
    # Team B cannot delete team A's fragment (its own project has no such entry).
    async with app_db.SessionFactory() as session:
        with pytest.raises(ServiceError) as exc:
            await memory_svc.delete_fragment(
                session, team_b, "billing-api", "redis-cache", frag_a
            )
    assert exc.value.code == "NOT_FOUND"
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_a, "billing-api", ["redis-cache"])
    assert len(got["entries"][0]["fragments"]) == 2  # intact
