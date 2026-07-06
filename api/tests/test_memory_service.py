"""Integration tests for the memory service layer (T1.3).

Every function is exercised against a real Postgres (testcontainers, via the
shared ``db_engine`` fixture): happy path + every declared error, the full
create -> append x3 -> get -> append -> compact cycle, the rule-4 survivor
guarantee, backpressure at the hard fragment limit, and the ``memory_history``
audit rows. Pure SQL, zero LLM (rule 2).
"""

from __future__ import annotations

import pytest
import tiktoken
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import db as app_db
from app.auth import create_team
from app.config import get_settings
from app.models import Memory, MemoryHistory
from app.services import memory
from app.services.memory import (
    ServiceError,
    append_fragment,
    compact_entry,
    create_entry,
    delete_entry,
    get_entries,
    list_entries,
)
from app.utils.tokens import count_tokens

PROJECT = "archivist"
OWNER = "enrico"

_SETTINGS = get_settings()


@pytest.fixture
async def team_id(db_engine: object) -> int:
    async with app_db.SessionFactory() as session:
        return await create_team(session, "acme")


# --- helpers ---------------------------------------------------------------


async def _create(team_id: int, keyword: str, content: str = "base content") -> dict:
    async with app_db.SessionFactory() as s:
        return await create_entry(s, team_id, PROJECT, keyword, content, OWNER)


async def _append(team_id: int, keyword: str, fragment: str = "a fragment") -> dict:
    async with app_db.SessionFactory() as s:
        return await append_fragment(s, team_id, PROJECT, keyword, fragment, OWNER)


async def _get(team_id: int, *keywords: str) -> dict:
    async with app_db.SessionFactory() as s:
        return await get_entries(s, team_id, PROJECT, list(keywords))


def _content_with_tokens(n: int) -> str:
    """Build content that encodes to exactly ``n`` cl100k_base tokens."""
    enc = tiktoken.get_encoding("cl100k_base")
    ids = enc.encode("word " * (n + 50))[:n]
    text = enc.decode(ids)
    assert count_tokens(text) == n  # guard against boundary drift
    return text


async def _history_versions(keyword: str) -> list[int]:
    async with app_db.SessionFactory() as s:
        mid = (
            await s.execute(
                select(Memory.id).where(Memory.keyword == keyword, Memory.deleted_at.is_(None))
            )
        ).scalar_one()
        rows = (
            (
                await s.execute(
                    select(MemoryHistory.version)
                    .where(MemoryHistory.memory_id == mid)
                    .order_by(MemoryHistory.version)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


# --- create_entry ----------------------------------------------------------


async def test_create_entry_happy_path(db_engine: object, team_id: int) -> None:
    result = await _create(team_id, "redis-cache", "cache eviction lives in redis")
    assert result == {
        "keyword": "redis-cache",
        "created": True,
        "version": 1,
        "token_count": count_tokens("cache eviction lives in redis"),
    }


async def test_create_entry_normalizes_keyword(db_engine: object, team_id: int) -> None:
    result = await _create(team_id, "Redis Cache", "x")
    assert result["keyword"] == "redis-cache"


async def test_create_entry_writes_history_row(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache")
    assert await _history_versions("redis-cache") == [1]


async def test_create_entry_auto_creates_project(db_engine: object, team_id: int) -> None:
    from app.models import Project

    await _create(team_id, "redis-cache")
    async with app_db.SessionFactory() as s:
        slug = (await s.execute(select(Project.slug).where(Project.slug == PROJECT))).scalar_one()
    assert slug == PROJECT


async def test_create_entry_duplicate_raises_already_exists(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache")
    with pytest.raises(ServiceError) as exc:
        await _create(team_id, "redis-cache")
    assert exc.value.code == "ALREADY_EXISTS"
    assert "memory_append" in exc.value.message


async def test_create_entry_over_cap_raises_entry_too_large(db_engine: object, team_id: int) -> None:
    big = _content_with_tokens(_SETTINGS.entry_token_hard_cap + 500)
    with pytest.raises(ServiceError) as exc:
        await _create(team_id, "too-big", big)
    assert exc.value.code == "ENTRY_TOO_LARGE"
    assert str(count_tokens(big)) in exc.value.message


async def test_create_existing_keyword_prefers_already_exists_over_too_large(
    db_engine: object, team_id: int
) -> None:
    # When a keyword already exists AND the new content is oversized, the agent
    # must be told ALREADY_EXISTS (→ memory_append), not ENTRY_TOO_LARGE: the
    # existence precondition is checked first (spec order).
    await _create(team_id, "redis-cache", "small")
    big = _content_with_tokens(_SETTINGS.entry_token_hard_cap + 500)
    with pytest.raises(ServiceError) as exc:
        await _create(team_id, "redis-cache", big)
    assert exc.value.code == "ALREADY_EXISTS"


async def test_create_at_soft_cap_has_no_flag(db_engine: object, team_id: int) -> None:
    content = _content_with_tokens(_SETTINGS.entry_token_cap)  # exactly 2000
    result = await _create(team_id, "at-soft", content)
    assert result["token_count"] == _SETTINGS.entry_token_cap
    assert "soft_cap_exceeded" not in result
    assert "warning" not in result


async def test_create_over_soft_cap_is_accepted_and_flagged(
    db_engine: object, team_id: int
) -> None:
    content = _content_with_tokens(3000)  # 2000 < 3000 < 10000
    result = await _create(team_id, "over-soft", content)
    assert result["created"] is True
    assert result["token_count"] == 3000
    assert result["soft_cap_exceeded"] is True
    assert "3000" in result["warning"]


async def test_create_at_hard_cap_is_accepted(db_engine: object, team_id: int) -> None:
    content = _content_with_tokens(_SETTINGS.entry_token_hard_cap)  # exactly 10000
    result = await _create(team_id, "at-hard", content)
    assert result["created"] is True
    assert result["soft_cap_exceeded"] is True


async def test_create_over_hard_cap_raises_entry_too_large(
    db_engine: object, team_id: int
) -> None:
    content = _content_with_tokens(_SETTINGS.entry_token_hard_cap + 1)  # 10001
    with pytest.raises(ServiceError) as exc:
        await _create(team_id, "over-hard", content)
    assert exc.value.code == "ENTRY_TOO_LARGE"
    assert str(count_tokens(content)) in exc.value.message


# --- append_fragment -------------------------------------------------------


async def test_append_happy_path(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache")
    result = await _append(team_id, "redis-cache", "evictions spiked at 2am")
    assert result["keyword"] == "redis-cache"
    assert isinstance(result["fragment_id"], int)
    assert result["pending_fragments"] == 1
    assert result["compaction_due"] is False


async def test_append_missing_entry_raises_not_found(db_engine: object, team_id: int) -> None:
    with pytest.raises(ServiceError) as exc:
        await _append(team_id, "ghost")
    assert exc.value.code == "NOT_FOUND"
    assert "memory_upsert" in exc.value.message


async def test_append_over_cap_raises_fragment_too_large(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache")
    big = "word " * (_SETTINGS.fragment_token_cap + 200)
    with pytest.raises(ServiceError) as exc:
        await _append(team_id, "redis-cache", big)
    assert exc.value.code == "FRAGMENT_TOO_LARGE"
    assert str(count_tokens(big)) in exc.value.message


async def test_append_never_touches_base_content(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache", "original base")
    await _append(team_id, "redis-cache", "a fragment")
    entry = (await _get(team_id, "redis-cache"))["entries"][0]
    assert entry["content"] == "original base"
    assert entry["version"] == 1


async def test_append_backpressure_at_hard_limit(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache")
    for _ in range(_SETTINGS.hard_limit_fragments):
        await _append(team_id, "redis-cache", "f")
    with pytest.raises(ServiceError) as exc:
        await _append(team_id, "redis-cache", "f")
    assert exc.value.code == "TOO_MANY_FRAGMENTS"
    assert "10 pending fragments" in exc.value.message
    # The rejected append inserts nothing: still exactly the hard limit.
    entry = (await _get(team_id, "redis-cache"))["entries"][0]
    assert len(entry["fragments"]) == _SETTINGS.hard_limit_fragments


# --- get_entries -----------------------------------------------------------


async def test_get_reports_missing_in_not_found(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache")
    result = await _get(team_id, "redis-cache", "ghost")
    assert [e["keyword"] for e in result["entries"]] == ["redis-cache"]
    assert result["not_found"] == ["ghost"]


async def test_get_shape_and_ordering(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache", "base")
    f1 = (await _append(team_id, "redis-cache", "frag one"))["fragment_id"]
    f2 = (await _append(team_id, "redis-cache", "frag two"))["fragment_id"]
    entry = (await _get(team_id, "redis-cache"))["entries"][0]
    assert entry["version"] == 1
    assert [f["id"] for f in entry["fragments"]] == [f1, f2]
    assert entry["fragments"][0]["created_by"] == OWNER
    assert entry["last_fragment_id"] == f2
    assert entry["token_total"] == count_tokens("base") + count_tokens("frag one") + count_tokens(
        "frag two"
    )


async def test_get_missing_project_all_not_found(db_engine: object, team_id: int) -> None:
    result = await _get(team_id, "redis-cache")
    assert result["entries"] == []
    assert result["not_found"] == ["redis-cache"]


# --- compaction_due rule (both branches) -----------------------------------


async def test_compaction_due_false_below_thresholds(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache", "small")
    for _ in range(_SETTINGS.compaction_fragment_threshold):
        r = await _append(team_id, "redis-cache", "f")
    assert r["pending_fragments"] == _SETTINGS.compaction_fragment_threshold
    assert r["compaction_due"] is False


async def test_compaction_due_true_over_fragment_threshold(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache", "small")
    for _ in range(_SETTINGS.compaction_fragment_threshold + 1):
        r = await _append(team_id, "redis-cache", "f")
    assert r["pending_fragments"] == _SETTINGS.compaction_fragment_threshold + 1
    assert r["compaction_due"] is True


async def test_compaction_due_true_over_token_ratio(db_engine: object, team_id: int) -> None:
    # One large-but-legal fragment pushes base+fragments over the token ratio
    # while pending (1) stays under the fragment threshold: exercises the OR.
    base = "word " * 1000  # < entry cap
    await _create(team_id, "redis-cache", base)
    frag = "word " * 400  # < fragment cap
    r = await _append(team_id, "redis-cache", frag)
    threshold = _SETTINGS.entry_token_cap * _SETTINGS.compaction_token_ratio
    assert count_tokens(base) + count_tokens(frag) > threshold
    assert r["pending_fragments"] == 1
    assert r["compaction_due"] is True


# --- compact_entry ---------------------------------------------------------


async def test_compact_happy_path_consumes_all(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache", "base")
    for _ in range(4):
        await _append(team_id, "redis-cache", "f")
    entry = (await _get(team_id, "redis-cache"))["entries"][0]
    async with app_db.SessionFactory() as s:
        result = await compact_entry(
            s, team_id, PROJECT, "redis-cache", "merged content", 1, entry["last_fragment_id"], OWNER
        )
    assert result == {
        "keyword": "redis-cache",
        "created": False,
        "version": 2,
        "token_count": count_tokens("merged content"),
        "fragments_consumed": 4,
    }
    after = (await _get(team_id, "redis-cache"))["entries"][0]
    assert after["fragments"] == []
    assert after["version"] == 2
    assert after["content"] == "merged content"
    assert after["last_fragment_id"] is None


async def test_compact_writes_history_row(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache", "base")
    await _append(team_id, "redis-cache", "f")
    entry = (await _get(team_id, "redis-cache"))["entries"][0]
    async with app_db.SessionFactory() as s:
        await compact_entry(
            s, team_id, PROJECT, "redis-cache", "merged", 1, entry["last_fragment_id"], OWNER
        )
    assert await _history_versions("redis-cache") == [1, 2]


async def test_compact_version_conflict(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache", "base")
    with pytest.raises(ServiceError) as exc:
        async with app_db.SessionFactory() as s:
            await compact_entry(s, team_id, PROJECT, "redis-cache", "merged", 99, None, OWNER)
    assert exc.value.code == "VERSION_CONFLICT"
    assert exc.value.extra["current_version"] == 1
    assert "expected 99" in exc.value.message and "current is 1" in exc.value.message


async def test_compact_missing_entry_not_found(db_engine: object, team_id: int) -> None:
    with pytest.raises(ServiceError) as exc:
        async with app_db.SessionFactory() as s:
            await compact_entry(s, team_id, PROJECT, "ghost", "merged", 1, None, OWNER)
    assert exc.value.code == "NOT_FOUND"


async def test_compact_over_cap_entry_too_large(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache", "base")
    big = _content_with_tokens(_SETTINGS.entry_token_hard_cap + 500)
    with pytest.raises(ServiceError) as exc:
        async with app_db.SessionFactory() as s:
            await compact_entry(s, team_id, PROJECT, "redis-cache", big, 1, None, OWNER)
    assert exc.value.code == "ENTRY_TOO_LARGE"


async def test_compact_over_soft_cap_is_accepted_and_flagged(
    db_engine: object, team_id: int
) -> None:
    await _create(team_id, "redis-cache", "base")
    f1 = (await _append(team_id, "redis-cache", "frag"))["fragment_id"]
    merged = _content_with_tokens(3000)
    async with app_db.SessionFactory() as s:
        result = await compact_entry(s, team_id, PROJECT, "redis-cache", merged, 1, f1, OWNER)
    assert result["version"] == 2
    assert result["fragments_consumed"] == 1
    assert result["soft_cap_exceeded"] is True
    assert "3000" in result["warning"]


async def test_compact_at_soft_cap_has_no_flag(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache", "base")
    merged = _content_with_tokens(_SETTINGS.entry_token_cap)  # exactly 2000
    async with app_db.SessionFactory() as s:
        result = await compact_entry(s, team_id, PROJECT, "redis-cache", merged, 1, None, OWNER)
    assert result["version"] == 2
    assert "soft_cap_exceeded" not in result


async def test_rule4_late_append_survives_compact(db_engine: object, team_id: int) -> None:
    # Fragment that lands AFTER the read but BEFORE the compact must survive.
    await _create(team_id, "redis-cache", "base")
    f1 = (await _append(team_id, "redis-cache", "early"))["fragment_id"]
    entry = (await _get(team_id, "redis-cache"))["entries"][0]  # reads last_fragment_id = f1
    assert entry["last_fragment_id"] == f1
    f2 = (await _append(team_id, "redis-cache", "late"))["fragment_id"]  # after the read
    async with app_db.SessionFactory() as s:
        result = await compact_entry(s, team_id, PROJECT, "redis-cache", "merged", 1, f1, OWNER)
    assert result["fragments_consumed"] == 1
    after = (await _get(team_id, "redis-cache"))["entries"][0]
    assert [f["id"] for f in after["fragments"]] == [f2]
    assert after["version"] == 2


# --- full cycle (DoD) ------------------------------------------------------


async def test_full_cycle(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache", "base")
    for _ in range(3):
        r = await _append(team_id, "redis-cache", "f")
    assert r["compaction_due"] is False  # 3 fragments, under threshold
    r = await _append(team_id, "redis-cache", "f")
    assert r["compaction_due"] is True  # 4th tips it over
    entry = (await _get(team_id, "redis-cache"))["entries"][0]
    async with app_db.SessionFactory() as s:
        result = await compact_entry(
            s, team_id, PROJECT, "redis-cache", "merged", 1, entry["last_fragment_id"], OWNER
        )
    assert result["fragments_consumed"] == 4
    after = (await _get(team_id, "redis-cache"))["entries"][0]
    assert after["fragments"] == []
    assert after["version"] == 2


# --- list_entries ----------------------------------------------------------


async def test_list_entries_ordered_with_metadata(db_engine: object, team_id: int) -> None:
    await _create(team_id, "beta", "b")
    await _create(team_id, "alpha", "a")
    await _append(team_id, "alpha", "f")
    async with app_db.SessionFactory() as s:
        result = await list_entries(s, team_id, PROJECT)
    assert result["project"] == PROJECT
    assert [e["keyword"] for e in result["entries"]] == ["alpha", "beta"]
    alpha = result["entries"][0]
    assert alpha["pending_fragments"] == 1
    assert alpha["compaction_due"] is False
    assert alpha["token_count"] == count_tokens("a")


async def test_list_entries_missing_project_is_empty(db_engine: object, team_id: int) -> None:
    async with app_db.SessionFactory() as s:
        result = await list_entries(s, team_id, PROJECT)
    assert result == {"project": PROJECT, "entries": []}


async def test_list_excludes_soft_deleted(db_engine: object, team_id: int) -> None:
    await _create(team_id, "alpha", "a")
    await _create(team_id, "beta", "b")
    async with app_db.SessionFactory() as s:
        await delete_entry(s, team_id, PROJECT, "beta", OWNER)
    async with app_db.SessionFactory() as s:
        result = await list_entries(s, team_id, PROJECT)
    assert [e["keyword"] for e in result["entries"]] == ["alpha"]


# --- delete_entry ----------------------------------------------------------


async def test_delete_happy_path(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache")
    async with app_db.SessionFactory() as s:
        result = await delete_entry(s, team_id, PROJECT, "redis-cache", OWNER)
    assert result == {"keyword": "redis-cache", "deleted": True}
    assert (await _get(team_id, "redis-cache"))["not_found"] == ["redis-cache"]


async def test_delete_missing_raises_not_found(db_engine: object, team_id: int) -> None:
    with pytest.raises(ServiceError) as exc:
        async with app_db.SessionFactory() as s:
            await delete_entry(s, team_id, PROJECT, "ghost", OWNER)
    assert exc.value.code == "NOT_FOUND"


async def test_recreate_after_delete_renames_old_row(db_engine: object, team_id: int) -> None:
    await _create(team_id, "redis-cache", "first")
    async with app_db.SessionFactory() as s:
        await delete_entry(s, team_id, PROJECT, "redis-cache", OWNER)
    # Re-creating the same keyword is allowed: the soft-deleted row is renamed
    # out of the way to free the unique (project_id, keyword) slot.
    await _create(team_id, "redis-cache", "second")
    entry = (await _get(team_id, "redis-cache"))["entries"][0]
    assert entry["content"] == "second"
    assert entry["version"] == 1

    async with app_db.SessionFactory() as s:
        rows = (
            await s.execute(select(Memory.keyword, Memory.deleted_at).order_by(Memory.id))
        ).all()
    assert len(rows) == 2
    old, new = rows
    assert old.deleted_at is not None
    assert old.keyword.startswith("redis-cache#deleted#")
    assert new.keyword == "redis-cache"
    assert new.deleted_at is None


# --- embedding_stale flag --------------------------------------------------


async def test_create_marks_embedding_stale(db_engine, team_id: int):
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        await memory.create_entry(s, team_id, "proj", "kw", "content", "alice")
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        row = (
            await s.execute(text("SELECT embedding_stale FROM memories WHERE keyword='kw'"))
        ).scalar_one()
        assert row is True


async def test_compact_sets_embedding_stale_again(db_engine, team_id: int):
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        await memory.create_entry(s, team_id, "proj", "kw", "v1", "alice")
        # Simulate the worker having embedded it: clear the flag.
        await s.execute(text("UPDATE memories SET embedding_stale=false WHERE keyword='kw'"))
        await s.commit()
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        await memory.compact_entry(s, team_id, "proj", "kw", "v2 merged", 1, None, "bob")
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        stale = (
            await s.execute(text("SELECT embedding_stale FROM memories WHERE keyword='kw'"))
        ).scalar_one()
        assert stale is True


# --- age_days / fragment_count ----------------------------------------------


async def test_get_entries_carries_age_days_and_fragment_count(team_id: int) -> None:
    await _create(team_id, "fresh")
    out = await _get(team_id, "fresh")
    entry = out["entries"][0]
    assert entry["age_days"] == 0
    assert entry["fragment_count"] == 0
