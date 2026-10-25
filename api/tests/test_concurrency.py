"""Concurrency tests: prove the append/compaction guarantees under real races.

Each scenario drives genuinely parallel sessions (separate connections, separate
transactions) against a testcontainers Postgres via ``asyncio.gather`` and is
repeated ``REPEATS`` times to surface flakes (20x without flakes).

Alongside the four core scenarios, these lock in the two concurrency
deferrals now fixed in ``app.services.memory``:

* ``_get_or_create_project`` — a brand-new-project race must resolve cleanly
  (one winner, the loser gets ``ALREADY_EXISTS``), never an uncaught
  ``IntegrityError``.
* ``append_fragment`` — the hard-limit backpressure gate must hold under race:
  two appends that both read ``pending = limit - 1`` may not both insert.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import db as app_db
from app import errors
from app.auth import create_team
from app.config import get_settings
from app.models import Memory, Project
from app.services import memory as svc

REPEATS = 20
_TABLES = ("api_keys", "memory_history", "memory_fragments", "memories", "projects")

# Enough pooled connections that a burst of appends (each holding its connection
# while it waits on the per-entry advisory lock) never starves the pool.
_POOL_SIZE = 32


@pytest.fixture
async def team_id(db_engine: object) -> int:
    async with app_db.SessionFactory() as session:
        return await create_team(session, "acme")


@pytest.fixture
async def factory(postgres_url: str) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """A session factory over a wide-pool engine on the shared container.

    Truncates once at setup; scenarios use a fresh keyword/project per iteration
    so the 20x loops stay isolated without re-truncating.
    """
    engine = create_async_engine(postgres_url, pool_size=_POOL_SIZE, max_overflow=0)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _in_session(
    factory: async_sessionmaker[AsyncSession],
    call: Callable[[AsyncSession], Awaitable[Any]],
) -> Any:
    """Run one service call in its own session (own connection/transaction)."""
    async with factory() as session:
        return await call(session)


def _classify(results: list[Any]) -> tuple[list[dict], list[svc.ServiceError], list[BaseException]]:
    """Split gathered results into successes, ServiceErrors and anything else."""
    oks = [r for r in results if isinstance(r, dict)]
    svc_errs = [r for r in results if isinstance(r, svc.ServiceError)]
    other = [
        r for r in results if isinstance(r, BaseException) and not isinstance(r, svc.ServiceError)
    ]
    return oks, svc_errs, other


# --- Scenario 1: concurrent appends up to the limit ------------------------


async def test_concurrent_appends_up_to_limit(
    factory: async_sessionmaker[AsyncSession], team_id: int
) -> None:
    """N == hard_limit concurrent appends → all succeed, N distinct fragments."""
    project = "concurrency"
    limit = get_settings().hard_limit_fragments
    for i in range(REPEATS):
        keyword = f"appends-{i}"
        await _in_session(
            factory, lambda s: svc.create_entry(s, team_id, project, keyword, "base", "creator")
        )

        results = await asyncio.gather(
            *(
                _in_session(
                    factory,
                    lambda s, n=n: svc.append_fragment(
                        s, team_id, project, keyword, f"frag-{n}", f"a-{n}"
                    ),
                )
                for n in range(limit)
            ),
            return_exceptions=True,
        )

        oks, svc_errs, other = _classify(results)
        assert not other, f"iter {i}: unexpected errors {other!r}"
        assert not svc_errs, f"iter {i}: unexpected rejections {[e.code for e in svc_errs]}"
        assert len(oks) == limit
        # Distinct fragment ids and no fragment lost.
        assert len({r["fragment_id"] for r in oks}) == limit
        count = await _in_session(
            factory, lambda s: svc.count_pending_fragments(s, team_id, project, keyword)
        )
        assert count == limit


# --- Deferral B: the hard limit holds under race ---------------------------


async def test_concurrent_appends_respect_hard_limit(
    factory: async_sessionmaker[AsyncSession], team_id: int
) -> None:
    """More concurrent appends than the cap → exactly ``limit`` land, rest rejected.

    Pre-fix, the count-then-insert gate let two appends that both read
    ``pending = limit - 1`` both insert, pushing the entry past the hard limit.
    """
    project = "concurrency"
    limit = get_settings().hard_limit_fragments
    burst = limit + 6
    for i in range(REPEATS):
        keyword = f"limit-{i}"
        await _in_session(
            factory, lambda s: svc.create_entry(s, team_id, project, keyword, "base", "creator")
        )

        results = await asyncio.gather(
            *(
                _in_session(
                    factory,
                    lambda s, n=n: svc.append_fragment(
                        s, team_id, project, keyword, f"frag-{n}", f"a-{n}"
                    ),
                )
                for n in range(burst)
            ),
            return_exceptions=True,
        )

        oks, svc_errs, other = _classify(results)
        assert not other, f"iter {i}: unexpected errors {other!r}"
        assert len(oks) == limit, f"iter {i}: {len(oks)} appends landed, expected {limit}"
        assert len(svc_errs) == burst - limit
        assert all(e.code == errors.TOO_MANY_FRAGMENTS for e in svc_errs)
        count = await _in_session(
            factory, lambda s: svc.count_pending_fragments(s, team_id, project, keyword)
        )
        assert count == limit, f"iter {i}: {count} fragments persisted, hard limit is {limit}"


# --- Scenario 2: concurrent compacts, same expected_version ----------------


async def test_concurrent_compacts_one_wins(
    factory: async_sessionmaker[AsyncSession], team_id: int
) -> None:
    """Two compacts at the same version → one wins, the other VERSION_CONFLICT."""
    project = "concurrency"
    for i in range(REPEATS):
        keyword = f"compact-{i}"
        await _in_session(
            factory, lambda s: svc.create_entry(s, team_id, project, keyword, "base", "creator")
        )
        appended = await _in_session(
            factory, lambda s: svc.append_fragment(s, team_id, project, keyword, "frag", "agent")
        )
        lfid = appended["fragment_id"]

        results = await asyncio.gather(
            _in_session(
                factory,
                lambda s: svc.compact_entry(
                    s, team_id, project, keyword, "merged-A", 1, lfid, "c-a"
                ),
            ),
            _in_session(
                factory,
                lambda s: svc.compact_entry(
                    s, team_id, project, keyword, "merged-B", 1, lfid, "c-b"
                ),
            ),
            return_exceptions=True,
        )

        oks, svc_errs, other = _classify(results)
        assert not other, f"iter {i}: unexpected errors {other!r}"
        assert len(oks) == 1, f"iter {i}: {len(oks)} compacts won, expected exactly one"
        assert oks[0]["version"] == 2
        assert len(svc_errs) == 1
        loser = svc_errs[0]
        assert loser.code == errors.VERSION_CONFLICT
        assert loser.extra["current_version"] == 2
        # The entry advanced exactly one version.
        got = await _in_session(factory, lambda s: svc.get_entries(s, team_id, project, [keyword]))
        assert got["entries"][0]["version"] == 2


# --- Scenario 3: append during compact is never lost -----------------------


async def test_append_during_compact_never_lost(
    factory: async_sessionmaker[AsyncSession], team_id: int
) -> None:
    """A fragment appended concurrently with a compact survives (id > cutoff)."""
    project = "concurrency"
    marker = "MARKER-e2f1a7c9"
    for i in range(REPEATS):
        keyword = f"race-{i}"
        await _in_session(
            factory, lambda s: svc.create_entry(s, team_id, project, keyword, "base", "creator")
        )
        old = await _in_session(
            factory,
            lambda s: svc.append_fragment(
                s, team_id, project, keyword, "old-fragment", "agent-old"
            ),
        )
        cutoff = old["fragment_id"]  # what the compactor "read": consume id <= cutoff

        results = await asyncio.gather(
            _in_session(
                factory,
                lambda s: svc.compact_entry(
                    s, team_id, project, keyword, "merged-base+old", 1, cutoff, "c"
                ),
            ),
            _in_session(
                factory,
                lambda s: svc.append_fragment(s, team_id, project, keyword, marker, "agent-marker"),
            ),
            return_exceptions=True,
        )

        oks, svc_errs, other = _classify(results)
        assert not other, f"iter {i}: unexpected errors {other!r}"
        assert not svc_errs, f"iter {i}: unexpected rejections {[e.code for e in svc_errs]}"

        got = await _in_session(factory, lambda s: svc.get_entries(s, team_id, project, [keyword]))
        entry = got["entries"][0]
        assert entry["version"] == 2  # compaction succeeded
        contents = [f["content"] for f in entry["fragments"]]
        # The marker (id > cutoff) survived; the old fragment (id <= cutoff) was consumed.
        assert marker in contents, f"iter {i}: appended marker was lost"
        assert "old-fragment" not in contents


# --- Scenario 4: concurrent create of the same new keyword -----------------


async def test_concurrent_create_one_wins(
    factory: async_sessionmaker[AsyncSession], team_id: int
) -> None:
    """Two creates of a brand-new project+keyword → one wins, other ALREADY_EXISTS.

    A fresh project slug each iteration also drives ``_get_or_create_project``'s
    brand-new-project race (pre-fix: an uncaught IntegrityError, not a clean
    ALREADY_EXISTS).
    """
    keyword = "shared"
    for i in range(REPEATS):
        project = f"newproj-{i}"

        results = await asyncio.gather(
            _in_session(
                factory,
                lambda s: svc.create_entry(s, team_id, project, keyword, "content-A", "agent-a"),
            ),
            _in_session(
                factory,
                lambda s: svc.create_entry(s, team_id, project, keyword, "content-B", "agent-b"),
            ),
            return_exceptions=True,
        )

        oks, svc_errs, other = _classify(results)
        assert not other, f"iter {i}: unexpected errors {other!r}"
        assert len(oks) == 1, f"iter {i}: {len(oks)} creates won, expected exactly one"
        assert oks[0]["created"] is True
        assert oks[0]["version"] == 1
        assert len(svc_errs) == 1
        assert svc_errs[0].code == errors.ALREADY_EXISTS

        # Exactly one project row and one entry — the race created no duplicates.
        nproj = await _in_session(
            factory,
            lambda s: s.scalar(
                select(func.count()).select_from(Project).where(Project.slug == project)
            ),
        )
        assert nproj == 1
        listing = await _in_session(factory, lambda s: svc.list_entries(s, team_id, project))
        assert len(listing["entries"]) == 1
        nmem = await _in_session(
            factory,
            lambda s: s.scalar(
                select(func.count())
                .select_from(Memory)
                .join(Project, Memory.project_id == Project.id)
                .where(Project.slug == project)
            ),
        )
        assert nmem == 1
