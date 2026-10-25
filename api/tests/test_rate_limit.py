"""Fixed-window rate limiting on the /mcp auth middleware.

The middleware allows ``RATE_LIMIT_PER_MINUTE`` requests per API key per
one-minute window; the next request in the same window → 429 with the canonical
``RATE_LIMITED`` message; a request in the following window passes
again. The limiter's clock (``auth._now``) is monkeypatched here so every burst
lands in a single, deterministic window with no real waiting.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.responses import JSONResponse
from httpx import ASGITransport

from app import auth
from app import db as app_db
from app.auth import RATE_LIMITED_MESSAGE, create_api_key, create_team, reset_rate_limiter
from app.config import get_settings
from app.main import create_app


def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://arc.test")


async def _new_key(owner: str = "alice", team: str = "acme") -> str:
    async with app_db.SessionFactory() as session:
        team_id = await create_team(session, team)
        return await create_api_key(session, owner, team_id)


def _with_probe(app: object) -> None:
    """A trivial 200 route under the protected /mcp prefix.

    It exercises the auth + rate-limit middleware without the MCP protocol
    overhead, so 120+ requests stay cheap. The mount at /mcp would shadow a
    route added after it, so move the probe ahead of the mount (as in test_auth).
    """

    async def _probe() -> JSONResponse:
        return JSONResponse({"ok": True})

    app.add_api_route("/mcp/probe", _probe, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())


async def test_rate_limit_blocks_over_the_window_then_resets(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_rate_limiter()
    limit = get_settings().rate_limit_per_minute
    assert limit == 120

    # Freeze the limiter's clock: every request below falls in one window.
    monkeypatch.setattr(auth, "_now", lambda: 1000.0)

    app = create_app()
    _with_probe(app)
    token = await _new_key()
    headers = {"Authorization": f"Bearer {token}"}

    async with _client(app) as client:
        # `limit` requests within the window all pass.
        for i in range(limit):
            response = await client.get("/mcp/probe", headers=headers)
            assert response.status_code == 200, f"request {i + 1} was {response.status_code}"

        # The next request in the same window → canonical 429.
        blocked = await client.get("/mcp/probe", headers=headers)
        assert blocked.status_code == 429
        body = blocked.json()
        assert body["error"]["code"] == "RATE_LIMITED"
        assert body["error"]["message"] == RATE_LIMITED_MESSAGE

        # Advance one minute → the fixed window rolls over, requests pass again.
        monkeypatch.setattr(auth, "_now", lambda: 1060.0)
        after = await client.get("/mcp/probe", headers=headers)
        assert after.status_code == 200


async def test_rate_limit_is_per_key(db_engine: object, monkeypatch: pytest.MonkeyPatch) -> None:
    reset_rate_limiter()
    limit = get_settings().rate_limit_per_minute
    monkeypatch.setattr(auth, "_now", lambda: 2000.0)

    app = create_app()
    _with_probe(app)
    key_a = await _new_key("alice", "acme-alice")
    key_b = await _new_key("bob", "acme-bob")

    async with _client(app) as client:
        for _ in range(limit):
            spent = await client.get("/mcp/probe", headers={"Authorization": f"Bearer {key_a}"})
            assert spent.status_code == 200
        # key_a is now over budget for this window...
        exhausted = await client.get("/mcp/probe", headers={"Authorization": f"Bearer {key_a}"})
        assert exhausted.status_code == 429
        # ...but key_b has its own independent counter.
        other = await client.get("/mcp/probe", headers={"Authorization": f"Bearer {key_b}"})
        assert other.status_code == 200
