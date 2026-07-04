"""Admin console report pages: stale entries and near-duplicate keywords."""

from __future__ import annotations

import httpx
from httpx import ASGITransport
from sqlalchemy import text

from app import db as app_db
from app.main import create_app
from app.services import memory as memory_svc


def _client(app: object) -> httpx.AsyncClient:
    # https base_url so the Secure/SameSite=strict session cookie is retained.
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="https://arc.test")


async def _login(client, plaintext) -> None:
    await client.post("/admin/login", data={"api_key": plaintext})


async def test_stale_report_lists_old_entry(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_id, "proj", "old-one", "x", "t")
        await memory_svc.create_entry(session, team_id, "proj", "fresh-one", "y", "t")
        await session.execute(
            text(
                "UPDATE memories SET updated_at = now() - interval '120 days' "
                "WHERE keyword = 'old-one'"
            )
        )
        await session.commit()
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        resp = await client.get("/admin/reports/stale")
    assert resp.status_code == 200
    assert "old-one" in resp.text


async def test_duplicates_report_lists_pair(db_engine, seed_team) -> None:
    vec = [1.0] + [0.0] * 383
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "proj", "dev-server", "a", "t", keyword_embedding=vec
        )
        await memory_svc.create_entry(
            session, team_id, "proj", "local-server", "b", "t", keyword_embedding=vec
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        resp = await client.get("/admin/reports/duplicates")
    assert resp.status_code == 200
    assert "dev-server" in resp.text and "local-server" in resp.text


async def test_reports_require_session(db_engine) -> None:
    app = create_app()
    async with _client(app) as client:
        resp = await client.get("/admin/reports/stale", follow_redirects=False)
    assert resp.status_code == 303


async def test_reports_are_team_scoped(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    other_team_id, _ = await seed_team("rivals")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, other_team_id, "proj", "old-one", "x", "t")
        await session.execute(
            text(
                "UPDATE memories SET updated_at = now() - interval '120 days' "
                "WHERE keyword = 'old-one'"
            )
        )
        await session.commit()
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        resp = await client.get("/admin/reports/stale")
    assert resp.status_code == 200
    assert "old-one" not in resp.text
