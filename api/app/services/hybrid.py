"""Hybrid retrieval: RRF fusion of the lexical retriever + a pgvector KNN.

Two-way Reciprocal Rank Fusion (k=60): the existing ``search_memories`` output is
ONE ranked list (its three tiers are one correlated retriever), fused with a
dense KNN list. Rank-based — raw lexical scores and cosine distances are never
mixed. The query embedding here is the only inline embedding, and it is on the
READ path only (never the write path). Enabled behind ``ENABLE_HYBRID_SEARCH``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.embeddings.base import Embedder
from app.log import get_logger
from app.models import Memory, MemoryFragment, Project
from app.services.search import search_memories
from app.utils.keywords import normalize_keyword

logger = get_logger()

K = TypeVar("K")
_RRF_K = 60
_EXCERPT_CHARS = 300


def rrf_fuse(ranked_lists: list[list[K]], k: int = _RRF_K) -> list[tuple[K, float]]:
    """Fuse ranked lists by Reciprocal Rank Fusion; return (key, score) best-first.

    ``score(key) = Σ 1 / (k + rank)`` with 1-based rank in each list it appears.
    Deterministic tie-break on the key itself.
    """
    scores: dict[K, float] = {}
    for lst in ranked_lists:
        for rank, key in enumerate(lst, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


async def _vector_knn(
    session: AsyncSession, qvec: list[float], team_id: int, project: str, scope: str, limit: int
) -> list[dict]:
    """Nearest entries by cosine distance, with the same fields as lexical rows.

    Always constrained to ``team_id``'s projects; ``scope="project"`` narrows
    further to the given project (resolved within the team).
    """
    pending = (
        select(func.count())
        .select_from(MemoryFragment)
        .where(MemoryFragment.memory_id == Memory.id)
        .scalar_subquery()
    )
    stmt = (
        select(
            Project.slug.label("project"),
            Memory.keyword,
            Memory.content,
            Memory.token_count,
            Memory.updated_at,
            pending.label("pending_fragments"),
        )
        .join(Project, Project.id == Memory.project_id)
        .where(
            Memory.deleted_at.is_(None),
            Memory.embedding.is_not(None),
            Project.team_id == team_id,
        )
        .order_by(Memory.embedding.cosine_distance(qvec))
        .limit(limit)
    )
    if scope == "project":
        pid = (
            await session.execute(
                select(Project.id).where(
                    Project.team_id == team_id, Project.slug == normalize_keyword(project)
                )
            )
        ).scalar_one_or_none()
        if pid is None:
            return []
        stmt = stmt.where(Memory.project_id == pid)

    rows = (await session.execute(stmt)).mappings().all()
    return [
        {
            "project": r["project"],
            "keyword": r["keyword"],
            "excerpt": (r["content"] or "")[:_EXCERPT_CHARS],
            "pending_fragments": r["pending_fragments"],
            "token_count": r["token_count"],
            "age_days": (datetime.now(timezone.utc) - r["updated_at"]).days,
        }
        for r in rows
    ]


async def hybrid_search(
    session: AsyncSession,
    embedder: Embedder,
    team_id: int,
    project: str,
    query: str,
    limit: int = 5,
    scope: str = "project",
) -> dict:
    """Lexical + vector retrieval fused by RRF; embedding-backed related keywords."""
    if scope not in ("project", "all"):
        raise ValueError(f"scope must be 'project' or 'all', got {scope!r}")

    candidate_k = get_settings().candidate_k
    lexical = await search_memories(session, team_id, project, query, limit=candidate_k, scope=scope)
    lex_results = lexical["results"]

    # Embedding is synchronous and CPU-bound (fastembed/ONNX); run it off the
    # event loop so a hybrid search never blocks the async read path. Documents
    # are embedded by the worker; this is the only inline (query) embedding.
    try:
        qvec = (await asyncio.to_thread(embedder.embed, [query]))[0]
    except Exception:
        # Symmetric with the document-side degrade (_vector_knn filters
        # embedding IS NOT NULL): an unavailable embedder at query time must
        # not error the whole search, it falls back to pure lexical ranking.
        logger.warning("hybrid_search: query embedding failed, degrading to lexical", exc_info=True)
        return {
            "results": lex_results[:limit],
            "related_keywords": lexical["related_keywords"],
            "vector_rescued": False,
        }
    vec_rows = await _vector_knn(session, qvec, team_id, project, scope, candidate_k)

    def key(r: dict) -> tuple[str, str]:
        return (r["project"], r["keyword"])

    lex_keys = [key(r) for r in lex_results]
    vec_keys = [key(r) for r in vec_rows]

    # Lexical row wins for metadata (it has a real ts_headline excerpt).
    by_key: dict[tuple[str, str], dict] = {}
    for r in vec_rows:
        by_key[key(r)] = r
    for r in lex_results:
        by_key[key(r)] = r

    fused = rrf_fuse([lex_keys, vec_keys])[:limit]
    lexical_topk = set(lex_keys[:limit])
    vector_rescued = any(k not in lexical_topk for k, _ in fused)

    results = []
    for k, score in fused:
        row = by_key[k]
        results.append(
            {
                "project": row["project"],
                "keyword": row["keyword"],
                "score": score,
                "excerpt": row["excerpt"],
                "pending_fragments": row["pending_fragments"],
                "token_count": row["token_count"],
                "age_days": row["age_days"],
            }
        )

    # Embedding-backed related_keywords: nearest neighbours not already returned.
    returned = {(r["project"], r["keyword"]) for r in results}
    related = [
        {"project": r["project"], "keyword": r["keyword"]}
        for r in vec_rows
        if (r["project"], r["keyword"]) not in returned
    ][: get_settings().search_limit_default]

    return {"results": results, "related_keywords": related, "vector_rescued": vector_rescued}
