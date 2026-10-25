"""Memory detail page shows content, fragments and history."""

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


async def test_memory_detail_shows_content_fragments_history(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "base content", "agent"
        )
    async with app_db.SessionFactory() as session:
        await memory_svc.append_fragment(
            session, team_id, "billing-api", "redis-cache", "extra fragment", "agent"
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        page = await client.get("/admin/memory/redis-cache", params={"project": "billing-api"})
    assert page.status_code == 200
    assert "base content" in page.text
    assert "extra fragment" in page.text


async def test_memory_detail_absent_returns_404(db_engine, seed_team) -> None:
    _, plaintext = await seed_team("acme")
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        page = await client.get("/admin/memory/ghost", params={"project": "billing-api"})
    assert page.status_code == 404
