"""Transactional memory service: create, get, append, compaction, list, delete.

Pure SQL, zero LLM in the critical path (rule 2). Each function receives an
``AsyncSession`` and commits its own unit of work (matching the convention in
``app.auth``). Keywords and project slugs are normalized server-side (rule 6);
every rejection is an actionable ``ServiceError`` carrying a canonical Appendix C
code + message (rule 7); the code + message texts come from ``app.errors``, the
single source (T1.6).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import errors
from app.config import get_settings
from app.models import Memory, MemoryFragment, MemoryHistory, Project
from app.utils.keywords import normalize_keyword
from app.utils.tokens import count_tokens


class ServiceError(Exception):
    """A rejection the caller must surface as an Appendix C tool error.

    ``extra`` carries structured fields for the JSON payload (e.g.
    ``current_version`` on a ``VERSION_CONFLICT``).
    """

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


# --- canonical error messages (codes + texts live in ``app.errors``) -------


def _not_found(keyword: str, project: str) -> ServiceError:
    return ServiceError(errors.NOT_FOUND, errors.not_found(keyword, project))


def _already_exists(keyword: str) -> ServiceError:
    return ServiceError(errors.ALREADY_EXISTS, errors.already_exists(keyword))


def _entry_too_large(tokens: int) -> ServiceError:
    return ServiceError(errors.ENTRY_TOO_LARGE, errors.entry_too_large(tokens))


def _fragment_too_large(tokens: int) -> ServiceError:
    return ServiceError(errors.FRAGMENT_TOO_LARGE, errors.fragment_too_large(tokens))


def _too_many_fragments(keyword: str) -> ServiceError:
    return ServiceError(errors.TOO_MANY_FRAGMENTS, errors.too_many_fragments(keyword))


def _version_conflict(expected: int, current: int) -> ServiceError:
    return ServiceError(
        errors.VERSION_CONFLICT,
        errors.version_conflict(expected, current),
        current_version=current,
    )


# --- shared helpers --------------------------------------------------------


def _compaction_due(pending: int, token_total: int) -> bool:
    """Single shared rule: many fragments OR the merged size nearing the cap."""
    settings = get_settings()
    return (
        pending > settings.compaction_fragment_threshold
        or token_total > settings.entry_token_cap * settings.compaction_token_ratio
    )


async def _project_id(session: AsyncSession, team_id: int, slug: str) -> int | None:
    return (
        await session.execute(
            select(Project.id).where(Project.team_id == team_id, Project.slug == slug)
        )
    ).scalar_one_or_none()


async def _get_or_create_project(session: AsyncSession, team_id: int, slug: str) -> int:
    pid = await _project_id(session, team_id, slug)
    if pid is not None:
        return pid
    # Brand-new-project race: two sessions can both observe the slug missing and
    # race to INSERT it. A bare INSERT would raise a unique violation on the
    # loser and abort its transaction; ON CONFLICT DO NOTHING keeps the
    # transaction usable. On conflict no row is returned, but DO NOTHING has
    # waited for the concurrent winner to commit, so a fresh read now sees the id.
    inserted = (
        await session.execute(
            pg_insert(Project)
            .values(team_id=team_id, slug=slug)
            .on_conflict_do_nothing(index_elements=["team_id", "slug"])
            .returning(Project.id)
        )
    ).scalar_one_or_none()
    if inserted is not None:
        return inserted
    pid = await _project_id(session, team_id, slug)
    if pid is None:  # pragma: no cover - DO NOTHING waited for the winner's commit
        raise RuntimeError(f"project {slug!r} vanished after ON CONFLICT DO NOTHING")
    return pid


async def _active_memory(
    session: AsyncSession, project_id: int, keyword: str, *, for_update: bool = False
) -> Memory | None:
    stmt = select(Memory).where(
        Memory.project_id == project_id,
        Memory.keyword == keyword,
        Memory.deleted_at.is_(None),
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


# --- write path ------------------------------------------------------------


async def create_entry(
    session: AsyncSession,
    team_id: int,
    project: str,
    keyword: str,
    content: str,
    updated_by: str,
    keyword_embedding: list[float] | None = None,
) -> dict:
    """Create a brand-new entry (version 1) + its history row. Commits.

    Raises ``ALREADY_EXISTS`` if an active entry holds the keyword, or
    ``ENTRY_TOO_LARGE`` when the content exceeds the entry cap. A soft-deleted
    row on the same keyword is renamed aside to free the unique slot.
    ``keyword_embedding``, when provided, is stored for near-duplicate detection;
    NULL rows are backfilled by the worker.
    """
    project = normalize_keyword(project)
    keyword = normalize_keyword(keyword)

    # Existence precondition first (spec order): a caller re-creating an existing
    # keyword must hear ALREADY_EXISTS (→ memory_append) even when the new
    # content is oversized. `_project_id` (lookup, not create) also keeps a
    # rejected create from leaving an orphan project behind.
    project_id = await _project_id(session, team_id, project)
    if project_id is not None and await _active_memory(session, project_id, keyword) is not None:
        raise _already_exists(keyword)

    tokens = count_tokens(content)
    if tokens > get_settings().entry_token_cap:
        raise _entry_too_large(tokens)

    if project_id is None:
        project_id = await _get_or_create_project(session, team_id, project)
    else:
        # Free the unique (project_id, keyword) slot from any soft-deleted row
        # (only reachable inside an already-existing project).
        stale = (
            (
                await session.execute(
                    select(Memory).where(
                        Memory.project_id == project_id,
                        Memory.keyword == keyword,
                        Memory.deleted_at.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in stale:
            row.keyword = f"{keyword}#deleted#{row.id}"
        if stale:
            await session.flush()

    memory = Memory(
        project_id=project_id,
        keyword=keyword,
        content=content,
        token_count=tokens,
        updated_by=updated_by,
        keyword_embedding=keyword_embedding,
    )
    session.add(memory)
    try:
        await session.flush()
    except IntegrityError as exc:  # concurrent create won the unique race
        await session.rollback()
        raise _already_exists(keyword) from exc

    session.add(
        MemoryHistory(memory_id=memory.id, version=1, content=content, updated_by=updated_by)
    )
    await session.commit()
    return {"keyword": keyword, "created": True, "version": 1, "token_count": tokens}


async def append_fragment(
    session: AsyncSession, team_id: int, project: str, keyword: str, fragment: str, created_by: str
) -> dict:
    """Queue a fragment as an append-only event. Commits.

    Never reads or rewrites the base content (rule 3). Raises ``NOT_FOUND`` if
    the entry is absent, ``FRAGMENT_TOO_LARGE`` over the fragment cap, or
    ``TOO_MANY_FRAGMENTS`` once the hard limit is reached (no insert then).
    """
    project = normalize_keyword(project)
    keyword = normalize_keyword(keyword)

    # Existence check without loading the base ``content`` (rule 3: the write
    # path neither reads nor rewrites base content). Only id + token_count are
    # needed — the latter for the compaction_due heuristic below.
    project_id = await _project_id(session, team_id, project)
    row = (
        None
        if project_id is None
        else (
            await session.execute(
                select(Memory.id, Memory.token_count).where(
                    Memory.project_id == project_id,
                    Memory.keyword == keyword,
                    Memory.deleted_at.is_(None),
                )
            )
        ).first()
    )
    if row is None:
        raise _not_found(keyword, project)
    memory_id, base_token_count = row.id, row.token_count

    tokens = count_tokens(fragment)
    if tokens > get_settings().fragment_token_cap:
        raise _fragment_too_large(tokens)

    # Serialize the count-then-insert for this entry so the hard-limit gate
    # can't be straddled by two racing appends that both read ``pending`` just
    # under the cap and both insert (rule 5 backpressure). A transaction-scoped
    # advisory lock keyed on the entry id releases at commit/rollback and lives
    # in a different lock space than compaction's row ``FOR UPDATE`` — so appends
    # still never block on, nor block, a concurrent compaction (rule 3).
    await session.execute(select(func.pg_advisory_xact_lock(memory_id)))

    pending = (
        await session.execute(select(func.count()).where(MemoryFragment.memory_id == memory_id))
    ).scalar_one()
    if pending >= get_settings().hard_limit_fragments:
        raise _too_many_fragments(keyword)

    frag = MemoryFragment(
        memory_id=memory_id, content=fragment, token_count=tokens, created_by=created_by
    )
    session.add(frag)
    await session.flush()
    await session.commit()

    pending += 1
    token_total = base_token_count + await _fragment_token_sum(session, memory_id)
    return {
        "keyword": keyword,
        "fragment_id": frag.id,
        "pending_fragments": pending,
        "compaction_due": _compaction_due(pending, token_total),
    }


async def compact_entry(
    session: AsyncSession,
    team_id: int,
    project: str,
    keyword: str,
    content: str,
    expected_version: int,
    last_fragment_id: int | None,
    updated_by: str,
) -> dict:
    """Merge-on-read compaction in a single transaction. Commits.

    Locks the entry ``FOR UPDATE``, checks the optimistic version, rewrites the
    content (version+1), consumes only fragments with ``id <= last_fragment_id``
    (a later append survives — rule 4), and records a history row.
    """
    project = normalize_keyword(project)
    keyword = normalize_keyword(keyword)

    project_id = await _project_id(session, team_id, project)
    memory = (
        None
        if project_id is None
        else await _active_memory(session, project_id, keyword, for_update=True)
    )
    if memory is None:
        raise _not_found(keyword, project)

    if memory.version != expected_version:
        current = memory.version
        await session.rollback()
        raise _version_conflict(expected_version, current)

    tokens = count_tokens(content)
    if tokens > get_settings().entry_token_cap:
        await session.rollback()
        raise _entry_too_large(tokens)

    consumed = 0
    if last_fragment_id is not None:
        result = await session.execute(
            delete(MemoryFragment).where(
                MemoryFragment.memory_id == memory.id,
                MemoryFragment.id <= last_fragment_id,
            )
        )
        consumed = result.rowcount or 0

    memory.content = content
    memory.version = expected_version + 1
    memory.token_count = tokens
    memory.updated_by = updated_by
    memory.updated_at = func.now()
    memory.embedding_stale = True

    session.add(
        MemoryHistory(
            memory_id=memory.id,
            version=memory.version,
            content=content,
            updated_by=updated_by,
        )
    )
    await session.commit()
    return {
        "keyword": keyword,
        "created": False,
        "version": expected_version + 1,
        "token_count": tokens,
        "fragments_consumed": consumed,
    }


async def delete_entry(
    session: AsyncSession, team_id: int, project: str, keyword: str, updated_by: str
) -> dict:
    """Soft-delete an entry (``deleted_at = now()``). Commits.

    Raises ``NOT_FOUND`` if there is no active entry for the keyword.
    """
    project = normalize_keyword(project)
    keyword = normalize_keyword(keyword)

    project_id = await _project_id(session, team_id, project)
    memory = None if project_id is None else await _active_memory(session, project_id, keyword)
    if memory is None:
        raise _not_found(keyword, project)

    memory.deleted_at = func.now()
    memory.updated_by = updated_by
    memory.updated_at = func.now()
    await session.commit()
    return {"keyword": keyword, "deleted": True}


# --- read path -------------------------------------------------------------


async def get_entries(
    session: AsyncSession, team_id: int, project: str, keywords: list[str]
) -> dict:
    """Return each requested entry with its fragments; missing → ``not_found``."""
    project = normalize_keyword(project)
    project_id = await _project_id(session, team_id, project)

    entries: list[dict] = []
    not_found: list[str] = []
    for raw in keywords:
        keyword = normalize_keyword(raw)
        memory = None if project_id is None else await _active_memory(session, project_id, keyword)
        if memory is None:
            not_found.append(keyword)
            continue

        fragments = (
            (
                await session.execute(
                    select(MemoryFragment)
                    .where(MemoryFragment.memory_id == memory.id)
                    .order_by(MemoryFragment.id)
                )
            )
            .scalars()
            .all()
        )
        frag_dicts = [
            {
                "id": f.id,
                "content": f.content,
                "created_by": f.created_by,
                "created_at": f.created_at,
            }
            for f in fragments
        ]
        token_total = memory.token_count + sum(f.token_count for f in fragments)
        last_fragment_id = fragments[-1].id if fragments else None
        entries.append(
            {
                "keyword": memory.keyword,
                "content": memory.content,
                "version": memory.version,
                "fragments": frag_dicts,
                "fragment_count": len(frag_dicts),
                "last_fragment_id": last_fragment_id,
                "compaction_due": _compaction_due(len(fragments), token_total),
                "token_total": token_total,
                "age_days": (datetime.now(timezone.utc) - memory.updated_at).days,
            }
        )
    return {"entries": entries, "not_found": not_found}


async def count_pending_fragments(
    session: AsyncSession, team_id: int, project: str, keyword: str
) -> int:
    """Return the pending-fragment count for an active entry (0 if it is absent).

    Read-only helper for the ``memory_upsert`` dispatch (T1.5): a compaction that
    omits ``last_fragment_id`` while fragments are pending would silently orphan
    them, so the tool layer uses this to reject that case with an instruction.
    """
    project = normalize_keyword(project)
    keyword = normalize_keyword(keyword)
    project_id = await _project_id(session, team_id, project)
    if project_id is None:
        return 0
    memory = await _active_memory(session, project_id, keyword)
    if memory is None:
        return 0
    return (
        await session.execute(select(func.count()).where(MemoryFragment.memory_id == memory.id))
    ).scalar_one()


async def list_entries(session: AsyncSession, team_id: int, project: str) -> dict:
    """List every active entry in a project, ordered by keyword, with metadata."""
    project = normalize_keyword(project)
    project_id = await _project_id(session, team_id, project)
    if project_id is None:
        return {"project": project, "entries": []}

    memories = (
        (
            await session.execute(
                select(Memory)
                .where(Memory.project_id == project_id, Memory.deleted_at.is_(None))
                .order_by(Memory.keyword)
            )
        )
        .scalars()
        .all()
    )

    entries: list[dict] = []
    for memory in memories:
        pending = (
            await session.execute(select(func.count()).where(MemoryFragment.memory_id == memory.id))
        ).scalar_one()
        token_total = memory.token_count + await _fragment_token_sum(session, memory.id)
        entries.append(
            {
                "keyword": memory.keyword,
                "token_count": memory.token_count,
                "pending_fragments": pending,
                "compaction_due": _compaction_due(pending, token_total),
                "updated_at": memory.updated_at,
            }
        )
    return {"project": project, "entries": entries}


async def _fragment_token_sum(session: AsyncSession, memory_id: int) -> int:
    return (
        await session.execute(
            select(func.coalesce(func.sum(MemoryFragment.token_count), 0)).where(
                MemoryFragment.memory_id == memory_id
            )
        )
    ).scalar_one()
