"""Async database engine, session factory and FastAPI dependency.

A single module-level ``AsyncEngine`` (connection pool) is shared process-wide;
``get_session`` yields a scoped ``AsyncSession`` for request/tool handlers and
guarantees it is closed afterwards.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

# ``pool_pre_ping`` avoids handing out connections the DB has already dropped
# (idle timeouts, restarts) — cheap insurance for a long-lived service.
engine: AsyncEngine = create_async_engine(get_settings().database_url, pool_pre_ping=True)

# ``expire_on_commit=False`` keeps ORM objects usable after commit, so handlers
# can serialize a result without triggering a lazy reload on a closed session.
SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an ``AsyncSession`` that is always closed."""
    async with SessionFactory() as session:
        yield session
