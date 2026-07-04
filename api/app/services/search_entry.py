"""Single search entry-point shared by the MCP tool and the admin console.

The hybrid-vs-lexical branch lived inline in ``mcp_server.memory_search``; both
callers now go through ``run_search`` so the admin console returns byte-for-byte
what the agent receives (search fidelity). ``embedder_provider`` is called
lazily — never in lexical mode, so the admin path stays embedding-free unless
hybrid is actually enabled.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.embeddings.base import Embedder, get_embedder
from app.services.hybrid import hybrid_search
from app.services.search import search_memories


async def run_search(
    session: AsyncSession,
    team_id: int,
    project: str,
    query: str,
    limit: int = 5,
    scope: str = "project",
    *,
    embedder_provider: Callable[[], Embedder] = get_embedder,
) -> dict:
    """Dispatch to ``hybrid_search`` when ``enable_hybrid_search`` else
    ``search_memories``. Returns the service dict verbatim (may include
    ``vector_rescued`` in hybrid mode). ``embedder_provider`` is called
    lazily, only in hybrid mode."""
    if get_settings().enable_hybrid_search:
        return await hybrid_search(
            session, embedder_provider(), team_id, project, query, limit, scope
        )
    return await search_memories(session, team_id, project, query, limit, scope)
