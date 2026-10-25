"""Admin search reproduces the MCP memory_search payload."""

from __future__ import annotations

import json

import httpx
from httpx import ASGITransport

from app import db as app_db
from app.main import create_app
from app.services import memory as memory_svc
from app.services.search_entry import run_search


def _client(app):
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="https://arc.test")


async def _login(client, plaintext):
    await client.post("/admin/login", data={"api_key": plaintext})


async def test_search_page_shows_payload_fields(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "about caching layers", "agent"
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        page = await client.get(
            "/admin/search", params={"project": "billing-api", "q": "caching", "scope": "project"}
        )
    assert page.status_code == 200
    assert "redis-cache" in page.text
    assert "score" in page.text  # rendered field label
    assert "MCP payload" in page.text  # the raw-JSON details panel


async def test_admin_payload_matches_run_search(db_engine, seed_team) -> None:
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session, team_id, "billing-api", "redis-cache", "about caching", "agent"
        )
    async with app_db.SessionFactory() as session:
        expected = await run_search(session, team_id, "billing-api", "caching", scope="project")
    expected.pop("vector_rescued", None)  # agent-facing payload strips it
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        page = await client.get(
            "/admin/search", params={"project": "billing-api", "q": "caching", "scope": "project"}
        )
    # The page embeds the exact JSON payload in a <script type="application/json" id="mcp-payload">
    marker = '<script type="application/json" id="mcp-payload">'
    body = page.text.split(marker, 1)[1].split("</script>", 1)[0]
    assert json.loads(body) == json.loads(json.dumps(expected, default=str))


async def test_search_project_scope_without_project_is_graceful(db_engine, seed_team) -> None:
    _, plaintext = await seed_team("acme")
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        # Default scope is "project" but no project param is given — a reachable
        # global entry point (e.g. /admin/search?q=foo typed directly).
        page = await client.get("/admin/search", params={"q": "foo"})
    assert page.status_code == 200


async def test_search_payload_escapes_script_breakout(db_engine, seed_team) -> None:
    """Stored-XSS regression: an excerpt containing a </script> breakout must not
    survive into the rendered page. The MCP payload is embedded via Jinja's
    `tojson` filter (which escapes `<`, `>`, `&` as \\uXXXX), not via
    `json.dumps(...) | safe`, which would let the browser close the <script>
    element early and execute the injected markup."""
    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(
            session,
            team_id,
            "billing-api",
            "xss-probe",
            "about caching </script><img src=x onerror=alert(1)>",
            "agent",
        )
    app = create_app()
    async with _client(app) as client:
        await _login(client, plaintext)
        page = await client.get(
            "/admin/search", params={"project": "billing-api", "q": "caching", "scope": "project"}
        )
    assert page.status_code == 200
    assert "</script><img" not in page.text


async def test_search_scope_all_is_team_bounded(db_engine, seed_team) -> None:
    team_a, key_a = await seed_team("acme")
    team_b, key_b = await seed_team("globex")
    async with app_db.SessionFactory() as session:
        await memory_svc.create_entry(session, team_a, "proj-a", "alpha", "team a secret", "agent")
        await memory_svc.create_entry(session, team_b, "proj-b", "beta", "team b secret", "agent")
    app = create_app()
    async with _client(app) as client:
        await _login(client, key_b)
        page = await client.get("/admin/search", params={"q": "secret", "scope": "all"})
    assert "beta" in page.text
    assert "alpha" not in page.text  # team A's entry never leaks
