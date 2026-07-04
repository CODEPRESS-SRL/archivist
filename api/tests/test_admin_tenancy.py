"""Cross-tenant isolation through the admin console routes.

Complements the per-route tenancy assertions already embedded in
``test_admin_delete.py`` (``test_delete_cannot_cross_teams``) with a single
consolidated pass over every admin surface: dashboard, project listing,
memory detail, delete, and compact. A session for team B must never see or
mutate team A's data, regardless of which route is used.
"""

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
    marker = 'name="csrf" value="'
    i = page.text.index(marker) + len(marker)
    return page.text[i : page.text.index('"', i)]


async def test_cross_tenant_isolation_through_admin_routes(db_engine, seed_team) -> None:
    team_a, _ = await seed_team("team-a")
    team_b, key_b = await seed_team("team-b")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_a, "billing-api", "redis-cache", "team a secret", "a"
        )
        # Team B's own entry (different project slug, so it never collides
        # with team A's "billing-api"), used only to mint a valid session
        # CSRF token.
        await memory_svc.create_entry(session, team_b, "internal-notes", "own-note", "note", "b")

    app = create_app()
    async with _client(app) as client:
        await _login(client, key_b)

        # 1. Dashboard does not list team A's project.
        dashboard = await client.get("/admin")
        assert dashboard.status_code == 200
        assert "billing-api" not in dashboard.text

        # 2. Project page for team A's slug shows no team-A entries (empty
        #    listing, since team B has no "billing-api" project of its own).
        project_page = await client.get("/admin/projects/billing-api")
        assert project_page.status_code == 200
        assert "redis-cache" not in project_page.text

        # 3. Memory detail for team A's keyword is invisible → 404.
        detail = await client.get("/admin/memory/redis-cache", params={"project": "billing-api"})
        assert detail.status_code == 404

        # Mint a valid session CSRF token via team B's own entry.
        token = await _csrf(client, "own-note", "internal-notes")

        # 4. Delete of team A's keyword, with team B's valid CSRF, leaves
        #    team A's entry intact (service treats it as NOT_FOUND).
        delete_resp = await client.post(
            "/admin/memory/redis-cache/delete",
            params={"project": "billing-api"},
            data={"csrf": token},
            follow_redirects=False,
        )
        assert delete_resp.status_code == 303

        # 5. Compact of team A's keyword, with team B's valid CSRF, leaves
        #    team A's entry unchanged.
        compact_resp = await client.post(
            "/admin/memory/redis-cache/compact",
            params={"project": "billing-api"},
            data={"csrf": token},
            follow_redirects=False,
        )
        assert compact_resp.status_code == 303

    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_a, "billing-api", ["redis-cache"])
    entry = got["entries"][0]
    assert entry["content"] == "team a secret"
    assert entry["version"] == 1
