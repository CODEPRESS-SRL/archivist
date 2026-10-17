"""Service-auth mode on /mcp, end to end against a real Postgres. [DOCKER]

``Authorization: Bearer <ARCHIVIST_SERVICE_SECRET>`` + ``X-Archivist-Team`` is
for the Workspace Node only. The ``arc_`` key path must keep working exactly as
before. The DB-free parts (pure functions, middleware with a stubbed lookup)
live in ``test_service_auth_unit.py``.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.responses import JSONResponse
from httpx import ASGITransport
from sqlalchemy import func, select

from app import auth
from app import db as app_db
from app.auth import UNAUTHORIZED_MESSAGE, create_team, current_key, reset_rate_limiter
from app.config import get_settings
from app.main import create_app
from app.models import Team

SECRET = "s" * 40


def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://arc.test")


def _app_with_probe(monkeypatch: pytest.MonkeyPatch, secret: str | None = SECRET):
    """App with a probe under /mcp echoing the auth context set by the middleware."""
    monkeypatch.setattr(get_settings(), "service_secret", secret)
    app = create_app()

    async def _probe() -> JSONResponse:
        ctx = current_key.get()
        return JSONResponse(
            {
                "team_name": ctx.team_name if ctx else None,
                "key_id": ctx.key_id if ctx else None,
                "owner": ctx.owner_name if ctx else None,
            }
        )

    app.add_api_route("/mcp/probe", _probe, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())
    return app


def _svc(team: str | None = "acme", secret: str = SECRET) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {secret}"}
    if team is not None:
        headers["X-Archivist-Team"] = team
    return headers


async def _team_count() -> int:
    async with app_db.SessionFactory() as session:
        return (await session.execute(select(func.count()).select_from(Team))).scalar_one()


async def test_secret_and_existing_team_passes_with_team_context(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with app_db.SessionFactory() as session:
        await create_team(session, "acme")
    app = _app_with_probe(monkeypatch)
    async with _client(app) as client:
        response = await client.get("/mcp/probe", headers=_svc("acme"))
    assert response.status_code == 200
    assert response.json() == {"team_name": "acme", "key_id": 0, "owner": "workspace"}


async def test_unknown_team_is_401_and_creates_nothing(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with app_db.SessionFactory() as session:
        await create_team(session, "acme")
    before = await _team_count()
    app = _app_with_probe(monkeypatch)
    async with _client(app) as client:
        response = await client.get("/mcp/probe", headers=_svc("ghost"))
    assert response.status_code == 401
    assert response.json()["error"]["message"] == UNAUTHORIZED_MESSAGE
    assert await _team_count() == before


async def test_wrong_secret_falls_to_key_path_and_is_401(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with app_db.SessionFactory() as session:
        await create_team(session, "acme")
    app = _app_with_probe(monkeypatch)
    async with _client(app) as client:
        response = await client.get("/mcp/probe", headers=_svc("acme", secret="w" * 40))
    assert response.status_code == 401
    assert response.json()["error"]["message"] == UNAUTHORIZED_MESSAGE


async def test_arc_key_still_works_identically(seed_team, monkeypatch: pytest.MonkeyPatch) -> None:
    _, plaintext = await seed_team("acme")
    app = _app_with_probe(monkeypatch)
    async with _client(app) as client:
        response = await client.get("/mcp/probe", headers={"Authorization": f"Bearer {plaintext}"})
    assert response.status_code == 200
    body = response.json()
    assert body["team_name"] == "acme"
    assert body["key_id"] != 0
    assert body["owner"] == "acme key"


@pytest.mark.parametrize("team", ["acme\r\nX-Evil: 1", "a" * 65])
async def test_malformed_team_header_is_rejected(
    db_engine: object, monkeypatch: pytest.MonkeyPatch, team: str
) -> None:
    async with app_db.SessionFactory() as session:
        await create_team(session, "acme")
    app = _app_with_probe(monkeypatch)
    # Drive the ASGI app directly: httpx refuses a CR/LF header value itself.
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "path": "/mcp/probe",
        "method": "GET",
        "headers": [
            (b"authorization", f"Bearer {SECRET}".encode()),
            (b"x-archivist-team", team.encode("latin-1")),
        ],
    }
    await auth.AuthMiddleware(app)(scope, receive, send)
    assert sent[0]["status"] == 401


async def test_service_calls_are_never_rate_limited_but_keys_are(
    seed_team, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_rate_limiter()
    monkeypatch.setattr(auth, "_now", lambda: 1000.0)
    limit = get_settings().rate_limit_per_minute
    _, plaintext = await seed_team("acme")
    app = _app_with_probe(monkeypatch)
    async with _client(app) as client:
        for i in range(130):
            ok = await client.get("/mcp/probe", headers=_svc("acme"))
            assert ok.status_code == 200, f"service call {i + 1} was {ok.status_code}"

        reset_rate_limiter()
        statuses = []
        for _ in range(130):
            r = await client.get("/mcp/probe", headers={"Authorization": f"Bearer {plaintext}"})
            statuses.append(r.status_code)
    assert statuses[:limit] == [200] * limit
    assert statuses[limit] == 429  # the 121st


@pytest.mark.parametrize("secret", [None, "", "x" * 31])
async def test_mode_off_when_secret_unset_or_short(
    db_engine: object, monkeypatch: pytest.MonkeyPatch, secret: str | None
) -> None:
    async with app_db.SessionFactory() as session:
        await create_team(session, "acme")
    app = _app_with_probe(monkeypatch, secret=secret)
    bearer = secret or "anything-at-all"
    async with _client(app) as client:
        response = await client.get("/mcp/probe", headers=_svc("acme", secret=bearer))
    assert response.status_code == 401
