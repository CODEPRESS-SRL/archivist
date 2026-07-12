"""Service-level delete_project: hard cascade delete, tenancy."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app import db as app_db
from app.models import Memory, MemoryFragment, MemoryHistory, Project
from app.services import memory as memory_svc
from app.services.memory import ServiceError


async def _count(model, **filters) -> int:
    async with app_db.SessionFactory() as session:
        stmt = select(func.count()).select_from(model)
        for col, val in filters.items():
            stmt = stmt.where(getattr(model, col) == val)
        return (await session.execute(stmt)).scalar_one()


async def test_delete_project_cascades_everything(db_engine, seed_team) -> None:
    team_id, _ = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_id, "billing-api", "redis-cache", "base", "agent")
        await memory_svc.append_fragment(
            session, team_id, "billing-api", "redis-cache", "frag", "agent"
        )
        # a second entry, soft-deleted, must also be removed
        await memory_svc.create_entry(session, team_id, "billing-api", "gone", "x", "agent")
        await memory_svc.delete_entry(session, team_id, "billing-api", "gone", "agent")

    out = None
    async with app_db.SessionFactory() as session:
        out = await memory_svc.delete_project(session, team_id, "billing-api")
    assert out["project"] == "billing-api"
    assert out["deleted"] is True
    assert out["entries_removed"] == 2

    async with app_db.SessionFactory() as session:
        pid = (
            await session.execute(select(Project.id).where(Project.slug == "billing-api"))
        ).scalar_one_or_none()
    assert pid is None
    assert await _count(Memory) == 0
    assert await _count(MemoryFragment) == 0
    assert await _count(MemoryHistory) == 0


async def test_delete_project_missing_raises_not_found(db_engine, seed_team) -> None:
    team_id, _ = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        with pytest.raises(ServiceError) as exc:
            await memory_svc.delete_project(session, team_id, "ghost")
    assert exc.value.code == "NOT_FOUND"


async def test_delete_project_is_tenant_isolated(db_engine, seed_team) -> None:
    team_a, _ = await seed_team("a")
    team_b, _ = await seed_team("b")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_a, "billing-api", "a-note", "x", "a")
    # Team B has no "billing-api" project → NOT_FOUND, team A untouched.
    async with app_db.SessionFactory() as session:
        with pytest.raises(ServiceError) as exc:
            await memory_svc.delete_project(session, team_b, "billing-api")
    assert exc.value.code == "NOT_FOUND"
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_a, "billing-api", ["a-note"])
    assert got["entries"] != []
