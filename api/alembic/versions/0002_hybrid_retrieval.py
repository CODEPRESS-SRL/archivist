"""hybrid retrieval — pgvector embedding columns + HNSW index

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-03

Adds the dense-retrieval columns to ``memories``: a 384-dim ``vector`` (matches
bge-small-en-v1.5 / Settings.embedding_dim), a ``embedding_stale`` dirty flag
defaulting true so every existing row is backfilled by the worker on first run,
and ``embedding_model`` for provenance. Downgrade is a full mirror; the
``vector`` extension is dropped on downgrade (``pg_trgm`` from 0001 is kept).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen on purpose: a migration is a historical record and must not import the
# live setting. The invariant "this == Settings.embedding_dim" is enforced at
# test time (test_embedding_column_dim_matches_settings); changing the app dim
# without a new migration fails the suite instead of silently drifting.
_DIM = 384


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column("memories", sa.Column("embedding", Vector(_DIM), nullable=True))
    op.add_column(
        "memories",
        sa.Column("embedding_stale", sa.Boolean, nullable=False, server_default=sa.text("true")),
    )
    op.add_column("memories", sa.Column("embedding_model", sa.Text, nullable=True))
    op.execute(
        "CREATE INDEX idx_memories_embedding_hnsw "
        "ON memories USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_memories_embedding_hnsw")
    op.drop_column("memories", "embedding_model")
    op.drop_column("memories", "embedding_stale")
    op.drop_column("memories", "embedding")
    op.execute("DROP EXTENSION IF EXISTS vector")
