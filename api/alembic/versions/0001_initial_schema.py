"""initial schema — 5 tables + FTS/trigram indexes

Revision ID: 0001
Revises:
Create Date: 2026-07-02

Hand-written (no autogenerate) so the DDL is exact: identity PKs, GENERATED STORED tsvector columns,
GIN full-text indexes and a pg_trgm keyword index. The downgrade is a full
mirror (drops every object this migration creates); the pg_trgm EXTENSION is
intentionally left in place (shared, safe to keep).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TSV = sa.Computed("to_tsvector('simple', content)", persisted=True)


def upgrade() -> None:
    # Required by the trigram (gin_trgm_ops) index on memories.keyword.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "projects",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("key_hash", sa.Text, nullable=False, unique=True),
        sa.Column("owner_name", sa.Text, nullable=False),
        sa.Column("revoked", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "memories",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger,
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("keyword", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_tsv", TSVECTOR, _TSV),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("token_count", sa.Integer, nullable=False),
        sa.Column("updated_by", sa.Text, nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("project_id", "keyword"),
    )
    op.create_index("idx_memories_tsv", "memories", ["content_tsv"], postgresql_using="gin")
    op.create_index(
        "idx_memories_keyword_trgm",
        "memories",
        ["keyword"],
        postgresql_using="gin",
        postgresql_ops={"keyword": "gin_trgm_ops"},
    )
    op.create_index(
        "idx_memories_project",
        "memories",
        ["project_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "memory_fragments",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column(
            "memory_id",
            sa.BigInteger,
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_tsv", TSVECTOR, _TSV),
        sa.Column("token_count", sa.Integer, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("idx_fragments_memory", "memory_fragments", ["memory_id"])
    op.create_index(
        "idx_fragments_tsv", "memory_fragments", ["content_tsv"], postgresql_using="gin"
    )

    op.create_table(
        "memory_history",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column(
            "memory_id",
            sa.BigInteger,
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("updated_by", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("idx_history_memory", "memory_history", ["memory_id"])


def downgrade() -> None:
    # Mirror of upgrade, reverse order. Dropping a table drops its indexes,
    # but we drop them explicitly to keep the mirror exact. The pg_trgm
    # EXTENSION is deliberately NOT dropped (see module docstring).
    op.drop_index("idx_history_memory", table_name="memory_history")
    op.drop_table("memory_history")

    op.drop_index("idx_fragments_tsv", table_name="memory_fragments")
    op.drop_index("idx_fragments_memory", table_name="memory_fragments")
    op.drop_table("memory_fragments")

    op.drop_index("idx_memories_project", table_name="memories")
    op.drop_index("idx_memories_keyword_trgm", table_name="memories")
    op.drop_index("idx_memories_tsv", table_name="memories")
    op.drop_table("memories")

    op.drop_table("api_keys")
    op.drop_table("projects")
