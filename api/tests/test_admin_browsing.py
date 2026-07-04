"""Dashboard exposes a global search entry-point."""

from __future__ import annotations

import httpx
from httpx import ASGITransport

from app import db as app_db
from app.main import create_app
from app.services import memory as memory_svc


def _client(app):
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="https://arc.test")


async def test_dashboard_has_global_search(db_engine, seed_team):
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_id, "billing-api", "redis-cache", "n", "agent")
    app = create_app()
    async with _client(app) as client:
        await client.post("/admin/login", data={"api_key": plaintext})
        page = await client.get("/admin")
    assert 'action="/admin/search"' in page.text
    assert 'name="scope"' in page.text  # scope=all carried on the global box
    assert 'value="all"' in page.text  # dashboard hardcodes scope=all
