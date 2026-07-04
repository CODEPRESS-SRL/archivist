"""Dashboard and project pages render team-scoped archive data."""

from __future__ import annotations

import httpx
from httpx import ASGITransport

from app import db as app_db
from app.main import create_app
from app.services import memory as memory_svc


def _client(app: object) -> httpx.AsyncClient:
    # https base_url so the Secure/SameSite=strict session cookie is retained.
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="https://arc.test")


async def _login(client, plaintext) -> None:
    await client.post("/admin/login", data={"api_key": plaintext})


async def test_dashboard_lists_projects(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "note", "agent"
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        page = await client.get("/admin")
    assert "billing-api" in page.text


async def test_project_page_lists_entries(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "note", "agent"
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        page = await client.get("/admin/projects/billing-api")
    assert page.status_code == 200
    assert "redis-cache" in page.text


async def test_project_search_filters(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "about caching", "agent"
        )
        await memory_svc.create_entry(
            session, team_id, "billing-api", "auth-flow", "about tokens", "agent"
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        page = await client.get("/admin/projects/billing-api", params={"q": "caching"})
    assert page.status_code == 200
    assert "redis-cache" in page.text
