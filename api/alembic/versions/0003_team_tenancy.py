"""team tenancy — teams table, team_id on api_keys/projects, per-team slug unique

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-03

Non-destructive backfill: add nullable team_id, park every existing row on a
'default' team, then enforce NOT NULL and swap projects UNIQUE(slug) for
UNIQUE(team_id, slug). Downgrade restores the global slug uniqueness (fails only
if two teams have meanwhile created the same slug — correct).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.add_column("api_keys", sa.Column("team_id", sa.BigInteger, nullable=True))
    op.add_column("projects", sa.Column("team_id", sa.BigInteger, nullable=True))

    # Backfill: one default team owns everything that predates tenancy.
    default_id = op.get_bind().execute(
        sa.text("INSERT INTO teams (name) VALUES ('default') RETURNING id")
    ).scalar_one()
    op.execute(sa.text("UPDATE api_keys SET team_id = :t").bindparams(t=default_id))
    op.execute(sa.text("UPDATE projects SET team_id = :t").bindparams(t=default_id))

    op.alter_column("api_keys", "team_id", nullable=False)
    op.alter_column("projects", "team_id", nullable=False)
    op.create_foreign_key("fk_api_keys_team", "api_keys", "teams", ["team_id"], ["id"])
    op.create_foreign_key("fk_projects_team", "projects", "teams", ["team_id"], ["id"])

    op.drop_constraint("projects_slug_key", "projects", type_="unique")
    op.create_unique_constraint("uq_projects_team_slug", "projects", ["team_id", "slug"])
    op.create_index("idx_api_keys_team", "api_keys", ["team_id"])


def downgrade() -> None:
    op.drop_index("idx_api_keys_team", table_name="api_keys")
    op.drop_constraint("uq_projects_team_slug", "projects", type_="unique")
    op.create_unique_constraint("projects_slug_key", "projects", ["slug"])
    op.drop_constraint("fk_projects_team", "projects", type_="foreignkey")
    op.drop_constraint("fk_api_keys_team", "api_keys", type_="foreignkey")
    op.drop_column("projects", "team_id")
    op.drop_column("api_keys", "team_id")
    op.drop_table("teams")
