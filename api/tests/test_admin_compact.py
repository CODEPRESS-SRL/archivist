"""Admin naïve compaction: merge fragments into content, consume atomically."""

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


async def test_compact_merges_and_consumes_fragments(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "base", "agent"
        )
    async with app_db.SessionFactory() as session:
        await memory_svc.append_fragment(
            session, team_id, "billing-api", "redis-cache", "frag-one", "agent"
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        token = await _csrf(client, "redis-cache", "billing-api")
        resp = await client.post(
            "/admin/memory/redis-cache/compact",
            params={"project": "billing-api"},
            data={"csrf": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_id, "billing-api", ["redis-cache"])
    entry = got["entries"][0]
    assert "base" in entry["content"] and "frag-one" in entry["content"]
    assert entry["fragments"] == []  # consumed
    assert entry["version"] == 2  # base v1 → merged v2


async def test_compact_no_pending_is_noop(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "base", "agent"
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        token = await _csrf(client, "redis-cache", "billing-api")
        resp = await client.post(
            "/admin/memory/redis-cache/compact",
            params={"project": "billing-api"},
            data={"csrf": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_id, "billing-api", ["redis-cache"])
    assert got["entries"][0]["version"] == 1  # untouched


async def test_compact_over_cap_surfaces_error(db_engine, seed_team, monkeypatch) -> None:
    from app.config import get_settings

    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "base", "agent"
        )
        await memory_svc.append_fragment(
            session, team_id, "billing-api", "redis-cache", "extra", "agent"
        )
    # shrink the cap so the merged content is over-cap
    monkeypatch.setattr(get_settings(), "entry_token_cap", 1)
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        token = await _csrf(client, "redis-cache", "billing-api")
        resp = await client.post(
            "/admin/memory/redis-cache/compact",
            params={"project": "billing-api"},
            data={"csrf": token},
            follow_redirects=True,
        )
    assert "Compaction failed" in resp.text
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_id, "billing-api", ["redis-cache"])
    assert got["entries"][0]["version"] == 1  # unchanged


async def test_compact_missing_csrf_field_is_400(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "base", "agent"
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        resp = await client.post(
            "/admin/memory/redis-cache/compact",
            params={"project": "billing-api"},
            data={},
            follow_redirects=False,
        )
        assert resp.status_code == 400


async def test_compact_not_found_redirects_without_error(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        # Only used to mint a valid session CSRF token; the compact target
        # below ("ghost") is never created.
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "base", "agent"
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        token = await _csrf(client, "redis-cache", "billing-api")
        resp = await client.post(
            "/admin/memory/ghost/compact",
            params={"project": "billing-api"},
            data={"csrf": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_id, "billing-api", ["redis-cache"])
    assert got["entries"][0]["version"] == 1  # untouched, no write happened


async def test_compact_rejects_bad_csrf(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "base", "agent"
        )
        await memory_svc.append_fragment(
            session, team_id, "billing-api", "redis-cache", "frag-one", "agent"
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        await _csrf(client, "redis-cache", "billing-api")  # establish a session token
        resp = await client.post(
            "/admin/memory/redis-cache/compact",
            params={"project": "billing-api"},
            data={"csrf": "wrong"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
    async with app_db.SessionFactory() as session:
        got = await memory_svc.get_entries(session, team_id, "billing-api", ["redis-cache"])
    assert got["entries"][0]["version"] == 1  # not compacted
