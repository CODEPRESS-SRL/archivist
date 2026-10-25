"""Semantic near-duplicate detection over keyword embeddings.

Pure SQL + pgvector, zero LLM. Called by the ``memory_upsert`` create path to
return an advisory ``near_duplicates`` list — never a rejection (the
write path never blocks).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Memory, Project
from app.utils.keywords import normalize_keyword

_PREVIEW_CHARS = 160


async def find_near_duplicates(
    session: AsyncSession,
    team_id: int,
    project: str,
    query_vec: list[float],
    exclude_keyword: str,
    threshold: float,
    limit: int = 3,
) -> list[dict]:
    """Nearest same-project keywords by cosine similarity, above ``threshold``.

    ``exclude_keyword`` (normalized) removes the just-created entry itself.
    Rows without a keyword_embedding are invisible (backfill catches them later).
    """
    project = normalize_keyword(project)
    distance = Memory.keyword_embedding.cosine_distance(query_vec)
    rows = (
        await session.execute(
            select(Memory.keyword, Memory.content, distance.label("distance"))
            .join(Project, Project.id == Memory.project_id)
            .where(
                Project.team_id == team_id,
                Project.slug == project,
                Memory.deleted_at.is_(None),
                Memory.keyword_embedding.is_not(None),
                Memory.keyword != normalize_keyword(exclude_keyword),
                distance <= 1.0 - threshold,
            )
            .order_by(distance)
            .limit(limit)
        )
    ).all()
    return [
        {
            "keyword": kw,
            "similarity": round(1.0 - float(dist), 4),
            "preview": (content or "")[:_PREVIEW_CHARS],
        }
        for kw, content, dist in rows
    ]
