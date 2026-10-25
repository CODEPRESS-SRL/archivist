"""unique team name — enforce UNIQUE(teams.name) for safe name-based lookup

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-03

Admin CLI's ``_resolve_team`` looks teams up by name with
``scalar_one_or_none()``, which assumes uniqueness. Without this constraint,
two teams sharing a name make that lookup raise ``MultipleResultsFound`` and
crash the CLI. Enforcing uniqueness in the schema is the fix; no application
code needs to defend against a case the database now prevents.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_teams_name", "teams", ["name"])


def downgrade() -> None:
    op.drop_constraint("uq_teams_name", "teams", type_="unique")
