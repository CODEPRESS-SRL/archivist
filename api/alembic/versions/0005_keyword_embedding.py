"""keyword_embedding — semantic near-duplicate detection on write

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-04

Adds ``memories.keyword_embedding`` (384-dim, nullable). Filled synchronously at
create time by the MCP tool layer; NULL rows (pre-existing, or created while the
embedder was unavailable) are backfilled by the worker. Keywords are immutable
after create, so no stale flag is needed. No index: near-dup lookups are
project-scoped and tiny.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen on purpose (see 0002): the invariant "this == Settings.embedding_dim"
# is enforced by test_keyword_embedding_column.
_DIM = 384


def upgrade() -> None:
    op.add_column("memories", sa.Column("keyword_embedding", Vector(_DIM), nullable=True))


def downgrade() -> None:
    op.drop_column("memories", "keyword_embedding")
