"""Admin route: delete an entire project (CSRF, tenancy, cascade)."""

from __future__ import annotations

from urllib.parse import quote

import httpx
from httpx import ASGITransport
from sqlalchemy import select

from app import db as app_db
from app.main import create_app
from app.models import Project
from app.services import memory as memory_svc


def _client(app):
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="https://arc.test")


async def _login(client, plaintext):
    await client.post("/admin/login", data={"api_key": plaintext})


async def _csrf_from_memory(client, keyword, project):
    # A memory page always embeds the session CSRF token in a hidden field.
    page = await client.get(f"/admin/memory/{keyword}", params={"project": project})
    marker = 'name="csrf" value="'
    i = page.text.index(marker) + len(marker)
    return page.text[i : page.text.index('"', i)]


async def _seed(team_id):
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "base", "agent"
        )


async def test_delete_project_happy_path(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    await _seed(team_id)
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        token = await _csrf_from_memory(client, "redis-cache", "billing-api")
        resp = await client.post(
            "/admin/projects/billing-api/delete",
            data={"csrf": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/admin?msg=")
    async with app_db.SessionFactory() as session:
        pid = (
            await session.execute(select(Project.id).where(Project.slug == "billing-api"))
        ).scalar_one_or_none()
    assert pid is None


async def test_delete_project_bad_csrf_is_400(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    await _seed(team_id)
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        await _csrf_from_memory(client, "redis-cache", "billing-api")
        resp = await client.post(
            "/admin/projects/billing-api/delete",
            data={"csrf": "wrong"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
    async with app_db.SessionFactory() as session:
        pid = (
            await session.execute(select(Project.id).where(Project.slug == "billing-api"))
        ).scalar_one_or_none()
    assert pid is not None  # not deleted


async def test_delete_project_cannot_cross_teams(db_engine, seed_team) -> None:
    team_a, _ = await seed_team("a")
    team_b, key_b = await seed_team("b")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_a, "billing-api", "a-note", "x", "a")
        await memory_svc.create_entry(session, team_b, "billing-api", "own", "x", "b")
    app = create_app()
    async with _client(app) as client:
        await _login(client, key_b)
        token = await _csrf_from_memory(client, "own", "billing-api")
        resp = await client.post(
            "/admin/projects/billing-api/delete",
            data={"csrf": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303  # team B deletes ITS OWN billing-api
    # Team A's identically-named project is untouched.
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_a, "billing-api", ["a-note"])
    assert got["entries"] != []


async def test_project_page_escapes_slug_in_js_context(db_engine, seed_team) -> None:
    _team_id, plaintext = await seed_team("acme")
    malicious = "x'-alert(document.cookie)-'"
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        resp = await client.get(f"/admin/projects/{quote(malicious, safe='')}")
        assert resp.status_code == 200
        # The raw single-quote breakout must never reach a JS execution context;
        # tojson emits the escaped ' form instead.
        assert "'-alert(document.cookie)-'" not in resp.text
        assert "\\u0027-alert(document.cookie)-\\u0027" in resp.text
