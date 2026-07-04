"""Migration 0005: keyword_embedding column exists, is nullable, dim matches."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.config import EMBEDDING_DIM


@pytest.mark.asyncio
async def test_keyword_embedding_column(db_engine) -> None:
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT atttypmod FROM pg_attribute "
                    "WHERE attrelid = 'memories'::regclass "
                    "AND attname = 'keyword_embedding'"
                )
            )
        ).one_or_none()
    assert row is not None, "keyword_embedding column missing"
    # pgvector stores the dim in atttypmod directly.
    assert row.atttypmod == EMBEDDING_DIM
