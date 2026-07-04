"""Shared test fixtures.

Integration tests run against a real Postgres via testcontainers (the
workbook's mandated approach). The schema is built by applying the actual
Alembic migration (the schema source of truth — catches drift that a
``create_all`` would hide). ``db_engine`` reroutes the application's
module-level engine/session factory to the container so the auth middleware and
the ``get_session`` dependency both hit the test DB, and truncates every table
between tests for isolation.
"""

from __future__ import annotations

import os

# Disable the Ryuk reaper: its sidecar container fails to map its port on
# Docker Desktop / Windows. The ``with PostgresContainer(...)`` block below
# tears the DB container down on its own, so the reaper is not needed.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

import subprocess  # noqa: E402
import sys  # noqa: E402
from collections.abc import AsyncGenerator, Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from app import auth as app_auth  # noqa: E402
from app import db as app_db  # noqa: E402

_API_DIR = Path(__file__).resolve().parent.parent
_TABLES = ("api_keys", "memory_history", "memory_fragments", "memories", "projects", "teams")


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """One migrated Postgres container for the whole session.

    Exposed as a fixture (not just its URL) so tests that shell out — e.g. the
    T3.2 backup/restore scripts via ``docker exec`` — can reach the container.
    """
    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as pg:
        # Apply the real migration (env.py reads DATABASE_URL via app settings).
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=_API_DIR,
            env={**os.environ, "DATABASE_URL": pg.get_connection_url()},
            check=True,
        )
        yield pg


@pytest.fixture(scope="session")
def postgres_url(postgres_container: PostgresContainer) -> str:
    """asyncpg URL for the migrated session container."""
    return postgres_container.get_connection_url()


@pytest.fixture
async def db_engine(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator:
    """Clean tables for this test; point ``app.db`` at the container."""
    engine = create_async_engine(postgres_url)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
    # TRUNCATE ... RESTART IDENTITY reuses api_key ids across tests, so clear the
    # process-local rate-limit counters too (they are keyed by api_key id).
    app_auth.reset_rate_limiter()
    from app.admin import auth as admin_auth

    admin_auth.reset_login_throttle()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # The middleware reads ``db.SessionFactory`` at call time; get_session reads
    # the module global — patching both here reroutes them to the test engine.
    monkeypatch.setattr(app_db, "engine", engine)
    monkeypatch.setattr(app_db, "SessionFactory", factory)
    yield engine
    await engine.dispose()


@pytest.fixture
def seed_team(db_engine: object):
    """Factory: create a team + one key, return (team_id, plaintext_key)."""
    from app.auth import create_api_key, create_team

    async def _make(name: str = "acme") -> tuple[int, str]:
        async with app_db.SessionFactory() as session:
            team_id = await create_team(session, name)
            plaintext = await create_api_key(session, f"{name} key", team_id)
        return team_id, plaintext

    return _make
