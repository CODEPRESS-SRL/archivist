"""Bearer API-key auth middleware on /mcp + key management CLI helpers.

Covers: no header / made-up key / revoked key → 401 with the canonical
message; valid key → request passes AND the owner is exposed via the
auth contextvar; the plaintext key is never persisted (only its SHA-256 hash);
/healthz stays unauthenticated.
"""

from __future__ import annotations

import hashlib

import httpx
from httpx import ASGITransport
from sqlalchemy import select

from app import db as app_db
from app.auth import (
    UNAUTHORIZED_MESSAGE,
    create_api_key,
    create_team,
    current_key,
    generate_api_key,
    hash_api_key,
    revoke_api_key,
)
from app.main import create_app
from app.models import ApiKey


def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://arc.test")


async def _new_key(owner: str = "alice", team: str = "acme") -> str:
    async with app_db.SessionFactory() as session:
        team_id = await create_team(session, team)
        return await create_api_key(session, owner, team_id)


# --- pure helpers (no DB) --------------------------------------------------


def test_generate_api_key_has_arc_prefix() -> None:
    key = generate_api_key()
    assert key.startswith("arc_")
    assert len(key) > len("arc_") + 20


def test_hash_api_key_is_sha256_hex() -> None:
    assert hash_api_key("arc_example") == hashlib.sha256(b"arc_example").hexdigest()


# --- middleware: rejection paths -------------------------------------------


async def test_missing_header_returns_401(db_engine: object) -> None:
    app = create_app()
    async with _client(app) as client:
        response = await client.post("/mcp")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert body["error"]["message"] == UNAUTHORIZED_MESSAGE


async def test_made_up_key_returns_401(db_engine: object) -> None:
    app = create_app()
    async with _client(app) as client:
        response = await client.post(
            "/mcp", headers={"Authorization": "Bearer arc_totally-made-up"}
        )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == UNAUTHORIZED_MESSAGE


async def test_revoked_key_returns_401(db_engine: object) -> None:
    plaintext = await _new_key("alice")
    async with app_db.SessionFactory() as session:
        key_id = (await session.execute(select(ApiKey.id))).scalar_one()
        assert await revoke_api_key(session, key_id) is True

    app = create_app()
    async with _client(app) as client:
        response = await client.post("/mcp", headers={"Authorization": f"Bearer {plaintext}"})
    assert response.status_code == 401


# --- middleware: success path ----------------------------------------------


async def test_valid_key_passes_and_exposes_owner(db_engine: object) -> None:
    from fastapi.responses import JSONResponse

    plaintext = await _new_key("alice")
    app = create_app()

    async def _probe() -> JSONResponse:
        ctx = current_key.get()
        return JSONResponse({"owner": ctx.owner_name if ctx else None})

    # A probe route under the protected prefix: reachable only past the auth
    # middleware, and it echoes what the middleware stored in the contextvar.
    # The MCP app is mounted at /mcp, which would shadow a probe added after it;
    # move the probe ahead of the mount so it stays reachable.
    app.add_api_route("/mcp/probe", _probe, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())

    async with _client(app) as client:
        response = await client.get("/mcp/probe", headers={"Authorization": f"Bearer {plaintext}"})
    assert response.status_code == 200
    assert response.json()["owner"] == "alice"


async def test_valid_key_exposes_team(db_engine: object) -> None:
    from fastapi.responses import JSONResponse

    plaintext = await _new_key("alice", "acme")
    app = create_app()

    async def _probe() -> JSONResponse:
        ctx = current_key.get()
        return JSONResponse(
            {"team_id": ctx.team_id if ctx else None, "team_name": ctx.team_name if ctx else None}
        )

    app.add_api_route("/mcp/probe", _probe, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())
    async with _client(app) as client:
        response = await client.get("/mcp/probe", headers={"Authorization": f"Bearer {plaintext}"})
    assert response.status_code == 200
    body = response.json()
    assert body["team_id"] is not None
    assert body["team_name"] == "acme"


async def test_healthz_stays_unauthenticated(db_engine: object) -> None:
    app = create_app()
    async with _client(app) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# --- key storage: plaintext is never persisted -----------------------------


async def test_plaintext_key_is_never_stored(db_engine: object) -> None:
    plaintext = await _new_key("alice")

    async with app_db.SessionFactory() as session:
        rows = (await session.execute(select(ApiKey))).scalars().all()

    # Grep every column of every api_keys row for the plaintext value.
    for row in rows:
        for value in (row.key_hash, row.owner_name):
            assert plaintext not in str(value)

    stored = rows[0].key_hash
    assert stored == hash_api_key(plaintext)
    assert stored != plaintext


async def test_created_key_authenticates(db_engine: object) -> None:
    # Mirrors `make apikey`: the printed plaintext must actually authenticate.
    plaintext = await _new_key("alice")
    app = create_app()
    # Run the app lifespan so the mounted /mcp session manager is started; a bare
    # POST (no MCP Accept/body) then gets past auth and is rejected by the MCP
    # endpoint (406), not by the middleware (401) — proving the key authenticates.
    async with app.router.lifespan_context(app), _client(app) as client:
        response = await client.post("/mcp", headers={"Authorization": f"Bearer {plaintext}"})
    assert response.status_code != 401


# --- lookup_auth_context: shared key->context lookup ------------------------


async def test_lookup_auth_context_returns_context_for_valid_key(seed_team) -> None:
    from app import db as app_db
    from app.auth import hash_api_key, lookup_auth_context

    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        ctx = await lookup_auth_context(session, hash_api_key(plaintext))
    assert ctx is not None
    assert ctx.team_id == team_id
    assert ctx.team_name == "acme"


async def test_lookup_auth_context_rejects_unknown_and_revoked(seed_team) -> None:
    from app import db as app_db
    from app.auth import hash_api_key, lookup_auth_context, revoke_api_key
    from app.models import ApiKey
    from sqlalchemy import select

    team_id, plaintext = await seed_team("acme")
    async with app_db.SessionFactory() as session:
        assert await lookup_auth_context(session, hash_api_key("nope")) is None
    async with app_db.SessionFactory() as session:
        key_id = (
            await session.execute(select(ApiKey.id).where(ApiKey.team_id == team_id))
        ).scalar_one()
        await revoke_api_key(session, key_id)
    async with app_db.SessionFactory() as session:
        assert await lookup_auth_context(session, hash_api_key(plaintext)) is None
