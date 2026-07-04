"""Read-only helpers backing the admin console: list_projects, get_history."""

from __future__ import annotations

from sqlalchemy import text

from app import db as app_db
from app.admin.reads import duplicate_pairs, get_history, list_projects, stale_entries
from app.services import memory as memory_svc


async def _seed(team_id: int) -> None:
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_id, "billing-api", "redis-cache", "v1", "agent")
    async with app_db.SessionFactory() as session:
        # a second version via naïve merge so history has 2 rows
        got = await memory_svc.get_entries(session, team_id, "billing-api", ["redis-cache"])
        entry = got["entries"][0]
        await memory_svc.compact_entry(
            session,
            team_id,
            "billing-api",
            "redis-cache",
            content="v2",
            expected_version=entry["version"],
            last_fragment_id=entry["last_fragment_id"],
            updated_by="admin:acme key",
        )


async def test_list_projects_counts_active_entries(db_engine, seed_team) -> None:
    team_id, _ = await seed_team("acme")
    await _seed(team_id)
    async with app_db.SessionFactory() as session:
        projects = await list_projects(session, team_id)
    assert projects == [{"slug": "billing-api", "memory_count": 1}]


async def test_list_projects_is_team_scoped(db_engine, seed_team) -> None:
    team_a, _ = await seed_team("a")
    team_b, _ = await seed_team("b")
    await _seed(team_a)
    async with app_db.SessionFactory() as session:
        assert await list_projects(session, team_b) == []


async def test_get_history_newest_first(db_engine, seed_team) -> None:
    team_id, _ = await seed_team("acme")
    await _seed(team_id)
    async with app_db.SessionFactory() as session:
        history = await get_history(session, team_id, "billing-api", "redis-cache")
    # create_entry also records a v1 history row in this codebase (see
    # app/services/memory.py create_entry + test_memory_service.py's
    # _history_versions helper), so get_history returns both versions,
    # newest first.
    assert [h["version"] for h in history] == [2, 1]
    assert history[0]["content"] == "v2"
    assert history[0]["updated_by"] == "admin:acme key"


async def test_get_history_absent_entry_is_empty(db_engine, seed_team) -> None:
    team_id, _ = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        assert await get_history(session, team_id, "billing-api", "ghost") == []


async def test_stale_entries_filters_and_orders(db_engine, seed_team) -> None:
    team_id, _ = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_id, "proj", "old-one", "x", "t")
        await memory_svc.create_entry(session, team_id, "proj", "fresh-one", "y", "t")
        await session.execute(
            text("UPDATE memories SET updated_at = now() - interval '120 days' WHERE keyword = 'old-one'")
        )
        await session.commit()
    async with app_db.SessionFactory() as session:
        rows = await stale_entries(session, team_id, min_age_days=90)
    assert [r["keyword"] for r in rows] == ["old-one"]
    assert rows[0]["age_days"] >= 120


async def test_duplicate_pairs_same_project_above_threshold(db_engine, seed_team) -> None:
    vec = [1.0] + [0.0] * 383
    team_id, _ = await seed_team("acme")
    other_team_id, _ = await seed_team("rivals")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_id, "proj", "dev-server", "a", "t", keyword_embedding=vec)
        await memory_svc.create_entry(session, team_id, "proj", "local-server", "b", "t", keyword_embedding=vec)
        await memory_svc.create_entry(session, team_id, "elsewhere", "dev-server", "c", "t", keyword_embedding=vec)
        await memory_svc.create_entry(session, other_team_id, "proj", "dev-server", "d", "t", keyword_embedding=vec)
    async with app_db.SessionFactory() as session:
        pairs = await duplicate_pairs(session, team_id, threshold=0.80)
    assert len(pairs) == 1  # only the same-team same-project pair
    assert {pairs[0]["keyword_a"], pairs[0]["keyword_b"]} == {"dev-server", "local-server"}
    assert pairs[0]["similarity"] >= 0.99
