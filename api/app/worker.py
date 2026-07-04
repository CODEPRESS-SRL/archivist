"""Async embedding worker — drains ``embedding_stale`` rows off the write path.

Runs as its own process/container (``python -m app.worker``): embedding is
CPU-bound and must not share the API's event loop. The write path only sets
``embedding_stale=true``; this worker claims dirty rows with ``FOR UPDATE SKIP
LOCKED`` (so multiple workers never double-claim), embeds the compacted base
content, writes the vector and clears the flag. Crash-safe: a failure before the
UPDATE leaves the flag set, so the row is retried next cycle (idempotent).
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.config import get_settings
from app.embeddings.base import Embedder, get_embedder
from app.log import configure_logging, get_logger
from app.models import Memory

logger = get_logger()


async def embed_batch(session: AsyncSession, embedder: Embedder, batch: int) -> int:
    """Embed up to ``batch`` stale, non-deleted entries. Returns the count."""
    rows = (
        await session.execute(
            select(Memory.id, Memory.content)
            .where(Memory.embedding_stale.is_(True), Memory.deleted_at.is_(None))
            .order_by(Memory.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if not rows:
        return 0

    vectors = embedder.embed([r.content for r in rows])
    for (mem_id, _content), vec in zip(rows, vectors, strict=True):
        await session.execute(
            update(Memory)
            .where(Memory.id == mem_id)
            .values(embedding=vec, embedding_stale=False, embedding_model=embedder.model_id)
        )
    await session.commit()
    logger.info("embed_batch", extra={"tool": "embed_worker", "outcome": "ok"})
    return len(rows)


async def embed_keyword_batch(session: AsyncSession, embedder: Embedder, batch: int) -> int:
    """Backfill ``keyword_embedding`` for rows where it is NULL. Returns the count.

    Covers pre-0005 rows and creates that ran while the embedder was down.
    Keywords are immutable, so NULL is the only dirty state (no stale flag).
    """
    rows = (
        await session.execute(
            select(Memory.id, Memory.keyword)
            .where(Memory.keyword_embedding.is_(None), Memory.deleted_at.is_(None))
            .order_by(Memory.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if not rows:
        return 0

    vectors = embedder.embed([r.keyword for r in rows])
    for (mem_id, _kw), vec in zip(rows, vectors, strict=True):
        await session.execute(
            update(Memory).where(Memory.id == mem_id).values(keyword_embedding=vec)
        )
    await session.commit()
    logger.info("embed_keyword_batch", extra={"tool": "embed_worker", "outcome": "ok"})
    return len(rows)


async def run_forever(
    embedder: Embedder, poll_seconds: float, batch: int
) -> None:  # pragma: no cover - infinite polling loop
    while True:
        try:
            async with db.SessionFactory() as session:
                embedded = await embed_batch(session, embedder, batch)
                embedded += await embed_keyword_batch(session, embedder, batch)
        except Exception:
            logger.exception("embed_cycle", extra={"tool": "embed_worker", "outcome": "error"})
            await asyncio.sleep(poll_seconds)
            continue
        if embedded == 0:
            await asyncio.sleep(poll_seconds)


def main() -> None:  # pragma: no cover - process entry point
    configure_logging()
    s = get_settings()
    asyncio.run(run_forever(get_embedder(), s.embed_poll_seconds, s.embed_batch_size))


if __name__ == "__main__":  # pragma: no cover
    main()
