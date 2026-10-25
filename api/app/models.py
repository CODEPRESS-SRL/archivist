"""SQLAlchemy models for the five Archivist tables.

These mirror the hand-written initial migration (``alembic/versions``) column
for column. The ``content_tsv`` columns are database-generated
(``GENERATED ALWAYS AS ... STORED``): they are declared with ``Computed`` so
SQLAlchemy treats them as read-only and NEVER emits them in INSERT/UPDATE —
Postgres populates them from ``content`` (pure SQL: no app-side FTS writes).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import EMBEDDING_DIM


class Base(DeclarativeBase):
    """Declarative base; ``Base.metadata`` is Alembic's ``target_metadata``."""


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("name", name="uq_teams_name"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("team_id", "slug", name="uq_projects_team_slug"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    team_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("teams.id"), nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)  # unique WITHIN a team
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # sha256 hex
    team_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("teams.id"), nullable=False)
    owner_name: Mapped[str] = mapped_column(Text, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=func.false())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (UniqueConstraint("project_id", "keyword"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    project_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("projects.id"), nullable=False)
    keyword: Mapped[str] = mapped_column(Text, nullable=False)  # normalized slug
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable to mirror the DDL exactly (the GENERATED column carries no
    # NOT NULL); Postgres always populates it, but the schema does not declare
    # it NOT NULL. `Mapped[str | None]` keeps autogenerate from drifting.
    content_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True)
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)
    embedding_stale: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=func.true()
    )
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Embedding of the keyword string itself (near-duplicate detection, S2).
    # Filled at create time; NULL rows are backfilled by the worker. Keywords
    # are immutable after create, so there is no stale flag.
    keyword_embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM), nullable=True
    )
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    fragments: Mapped[list[MemoryFragment]] = relationship(
        back_populates="memory", cascade="all, delete-orphan"
    )
    history: Mapped[list[MemoryHistory]] = relationship(
        back_populates="memory", cascade="all, delete-orphan"
    )


class MemoryFragment(Base):
    __tablename__ = "memory_fragments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    memory_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable to mirror the DDL exactly (the GENERATED column carries no
    # NOT NULL); Postgres always populates it, but the schema does not declare
    # it NOT NULL. `Mapped[str | None]` keeps autogenerate from drifting.
    content_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True)
    )
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    memory: Mapped[Memory] = relationship(back_populates="fragments")


class MemoryHistory(Base):
    __tablename__ = "memory_history"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    memory_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    memory: Mapped[Memory] = relationship(back_populates="history")
