import pytest
from sqlalchemy import text

from app.config import Settings


@pytest.mark.asyncio
async def test_hybrid_schema_present(db_engine):
    async with db_engine.connect() as conn:
        cols = (
            (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='memories'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {"embedding", "embedding_stale", "embedding_model"} <= set(cols)

        ext = (
            await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'"))
        ).scalar_one_or_none()
        assert ext == 1

        idx = (
            await conn.execute(
                text("SELECT 1 FROM pg_indexes WHERE indexname='idx_memories_embedding_hnsw'")
            )
        ).scalar_one_or_none()
        assert idx == 1

        # pg_trgm from 0001 survives.
        trgm = (
            await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='pg_trgm'"))
        ).scalar_one_or_none()
        assert trgm == 1


@pytest.mark.asyncio
async def test_embedding_column_dim_matches_settings(db_engine):
    """The migrated ``vector(N)`` column and ``Settings.embedding_dim`` must
    agree; a drift would only surface as a confusing pgvector error at query
    time. For the vector type, atttypmod is the dimension directly."""
    async with db_engine.connect() as conn:
        typmod = (
            await conn.execute(
                text(
                    "SELECT a.atttypmod FROM pg_attribute a "
                    "JOIN pg_class c ON a.attrelid = c.oid "
                    "WHERE c.relname = 'memories' AND a.attname = 'embedding'"
                )
            )
        ).scalar_one()
    assert typmod == Settings().embedding_dim
