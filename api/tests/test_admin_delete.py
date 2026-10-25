"""Admin delete action: soft-delete, CSRF, tenancy."""

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


async def _csrf(client, keyword, project) -> str:
    page = await client.get(f"/admin/memory/{keyword}", params={"project": project})
    # csrf token is embedded in the hidden field
    marker = 'name="csrf" value="'
    i = page.text.index(marker) + len(marker)
    return page.text[i : page.text.index('"', i)]


async def test_delete_soft_deletes_and_redirects(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "note", "agent"
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        token = await _csrf(client, "redis-cache", "billing-api")
        resp = await client.post(
            "/admin/memory/redis-cache/delete",
            params={"project": "billing-api"},
            data={"csrf": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_id, "billing-api", ["redis-cache"])
    assert got["entries"] == []


async def test_delete_rejects_bad_csrf(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "note", "agent"
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        await _csrf(client, "redis-cache", "billing-api")  # establish a session token
        resp = await client.post(
            "/admin/memory/redis-cache/delete",
            params={"project": "billing-api"},
            data={"csrf": "wrong"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_id, "billing-api", ["redis-cache"])
    assert got["entries"] != []  # not deleted


async def test_delete_missing_csrf_field_is_400(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "note", "agent"
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        resp = await client.post(
            "/admin/memory/redis-cache/delete",
            params={"project": "billing-api"},
            data={},
            follow_redirects=False,
        )
        assert resp.status_code == 400


async def test_delete_cannot_cross_teams(db_engine, seed_team) -> None:
    team_a, _ = await seed_team("a")
    team_b, key_b = await seed_team("b")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_a, "billing-api", "redis-cache", "note", "a")
        await memory_svc.create_entry(session, team_b, "billing-api", "own-note", "note", "b")
    app = create_app()
    async with _client(app) as client:
        await _login(client, key_b)
        # Mint a valid session CSRF token for team B by visiting its own memory page.
        token = await _csrf(client, "own-note", "billing-api")
        resp = await client.post(
            "/admin/memory/redis-cache/delete",
            params={"project": "billing-api"},
            data={"csrf": token},
            follow_redirects=False,
        )
        # Team B's csrf is valid, but team A's keyword is invisible to team B's
        # tenant scope, so the service raises NOT_FOUND and we redirect, not 500.
        assert resp.status_code == 303
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_a, "billing-api", ["redis-cache"])
    assert got["entries"] != []  # team A's entry is intact
