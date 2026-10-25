"""Admin route: delete a single pending fragment (CSRF, tenancy)."""

from __future__ import annotations

import httpx
from httpx import ASGITransport

from app import db as app_db
from app.main import create_app
from app.services import memory as memory_svc


def _client(app):
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="https://arc.test")


async def _login(client, plaintext):
    await client.post("/admin/login", data={"api_key": plaintext})


async def _csrf(client, keyword, project):
    page = await client.get(f"/admin/memory/{keyword}", params={"project": project})
    marker = 'name="csrf" value="'
    i = page.text.index(marker) + len(marker)
    return page.text[i : page.text.index('"', i)]


async def _seed(team_id):
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "base", "agent"
        )
        f = await memory_svc.append_fragment(
            session, team_id, "billing-api", "redis-cache", "frag", "agent"
        )
    return f["fragment_id"]


async def test_delete_fragment_happy_path(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    frag_id = await _seed(team_id)
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        token = await _csrf(client, "redis-cache", "billing-api")
        resp = await client.post(
            f"/admin/memory/redis-cache/fragments/{frag_id}/delete",
            params={"project": "billing-api"},
            data={"csrf": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_id, "billing-api", ["redis-cache"])
    assert got["entries"][0]["fragments"] == []


async def test_delete_fragment_bad_csrf_is_400(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    frag_id = await _seed(team_id)
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        await _csrf(client, "redis-cache", "billing-api")
        resp = await client.post(
            f"/admin/memory/redis-cache/fragments/{frag_id}/delete",
            params={"project": "billing-api"},
            data={"csrf": "wrong"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_id, "billing-api", ["redis-cache"])
    assert len(got["entries"][0]["fragments"]) == 1  # not deleted


async def test_delete_fragment_missing_redirects_with_flash(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    await _seed(team_id)
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        token = await _csrf(client, "redis-cache", "billing-api")
        resp = await client.post(
            "/admin/memory/redis-cache/fragments/999999/delete",
            params={"project": "billing-api"},
            data={"csrf": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303  # friendly flash, not a 500
