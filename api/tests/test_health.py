"""/healthz returns 200 when the DB answers and 503 when it does not."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.db import get_session
from app.main import create_app


class _OkSession:
    async def execute(self, _stmt: object) -> None:
        return None


class _BrokenSession:
    async def execute(self, _stmt: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("db is down"))


async def _ok_session() -> AsyncGenerator[_OkSession, None]:
    yield _OkSession()


async def _broken_session() -> AsyncGenerator[_BrokenSession, None]:
    yield _BrokenSession()


def test_healthz_ok() -> None:
    app = create_app()
    app.dependency_overrides[get_session] = _ok_session
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


def test_healthz_db_down_returns_503_with_detail() -> None:
    app = create_app()
    app.dependency_overrides[get_session] = _broken_session
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["db"] == "error"
    assert "detail" in body


def test_healthz_requires_no_auth_header() -> None:
    app = create_app()
    app.dependency_overrides[get_session] = _ok_session
    with TestClient(app) as client:
        # No Authorization header sent — must still succeed.
        response = client.get("/healthz")
    assert response.status_code == 200
