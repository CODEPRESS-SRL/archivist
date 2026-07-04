"""Read-only queries backing the admin console (team-scoped, no writes)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Memory, MemoryHistory, Project
from app.utils.keywords import normalize_keyword


async def list_projects(session: AsyncSession, team_id: int) -> list[dict]:
    """Projects of a team with their active-entry count, ordered by slug."""
    rows = (
        await session.execute(
            select(Project.slug, func.count(Memory.id))
            .select_from(Project)
            .outerjoin(
                Memory,
                and_(Memory.project_id == Project.id, Memory.deleted_at.is_(None)),
            )
            .where(Project.team_id == team_id)
            .group_by(Project.slug)
            .order_by(Project.slug)
        )
    ).all()
    return [{"slug": slug, "memory_count": count} for slug, count in rows]


async def get_history(
    session: AsyncSession, team_id: int, project: str, keyword: str
) -> list[dict]:
    """Version history of an active entry, newest first; [] if absent."""
    project = normalize_keyword(project)
    keyword = normalize_keyword(keyword)
    memory_id = (
        await session.execute(
            select(Memory.id)
            .join(Project, Project.id == Memory.project_id)
            .where(
                Project.team_id == team_id,
                Project.slug == project,
                Memory.keyword == keyword,
                Memory.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if memory_id is None:
        return []
    rows = (
        await session.execute(
            select(
                MemoryHistory.version,
                MemoryHistory.content,
                MemoryHistory.updated_by,
                MemoryHistory.created_at,
            )
            .where(MemoryHistory.memory_id == memory_id)
            .order_by(MemoryHistory.version.desc())
        )
    ).all()
    return [{"version": v, "content": c, "updated_by": u, "created_at": ts} for v, c, u, ts in rows]


async def stale_entries(session: AsyncSession, team_id: int, min_age_days: int) -> list[dict]:
    """Active entries not updated in ``min_age_days``, oldest first (S4)."""
    cutoff = func.now() - text(f"interval '{int(min_age_days)} days'")
    rows = (
        await session.execute(
            select(Project.slug, Memory.keyword, Memory.updated_at)
            .join(Project, Project.id == Memory.project_id)
            .where(
                Project.team_id == team_id,
                Memory.deleted_at.is_(None),
                Memory.updated_at < cutoff,
            )
            .order_by(Memory.updated_at)
        )
    ).all()

    now = datetime.now(timezone.utc)
    return [
        {"project": slug, "keyword": kw, "updated_at": ts, "age_days": (now - ts).days}
        for slug, kw, ts in rows
    ]


async def duplicate_pairs(
    session: AsyncSession, team_id: int, threshold: float, limit: int = 50
) -> list[dict]:
    """Same-project keyword pairs above cosine ``threshold`` — merge candidates (S4)."""
    a, b = aliased(Memory), aliased(Memory)
    distance = a.keyword_embedding.cosine_distance(b.keyword_embedding)
    rows = (
        await session.execute(
            select(Project.slug, a.keyword, b.keyword, distance.label("distance"))
            .join(Project, Project.id == a.project_id)
            .where(
                Project.team_id == team_id,
                b.project_id == a.project_id,
                a.id < b.id,
                a.deleted_at.is_(None),
                b.deleted_at.is_(None),
                a.keyword_embedding.is_not(None),
                b.keyword_embedding.is_not(None),
                distance <= 1.0 - threshold,
            )
            .order_by(distance)
            .limit(limit)
        )
    ).all()
    return [
        {
            "project": slug,
            "keyword_a": ka,
            "keyword_b": kb,
            "similarity": round(1.0 - float(dist), 4),
        }
        for slug, ka, kb, dist in rows
    ]
