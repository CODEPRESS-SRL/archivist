"""0002 tenancy migration: teams table + team_id backfill + unique swap."""
from __future__ import annotations

from sqlalchemy import text

from app import db as app_db


async def test_projects_unique_is_per_team(db_engine: object) -> None:
    # Same slug under two different teams must coexist; duplicate within one team must fail.
    async with app_db.SessionFactory() as session:
        t1 = (await session.execute(text("INSERT INTO teams (name) VALUES ('t1') RETURNING id"))).scalar_one()
        t2 = (await session.execute(text("INSERT INTO teams (name) VALUES ('t2') RETURNING id"))).scalar_one()
        await session.execute(text("INSERT INTO projects (slug, team_id) VALUES ('billing-api', :t)"), {"t": t1})
        await session.execute(text("INSERT INTO projects (slug, team_id) VALUES ('billing-api', :t)"), {"t": t2})
        await session.commit()  # cross-team duplicate slug: OK

    async with app_db.SessionFactory() as session:
        with_error = False
        try:
            await session.execute(text("INSERT INTO projects (slug, team_id) VALUES ('billing-api', :t)"), {"t": t1})
            await session.commit()
        except Exception:
            with_error = True
        assert with_error, "same slug twice in one team must violate UNIQUE(team_id, slug)"


async def test_api_keys_have_team_id_not_null(db_engine: object) -> None:
    async with app_db.SessionFactory() as session:
        cols = (await session.execute(text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name='api_keys' AND column_name='team_id'"
        ))).mappings().all()
    assert cols and cols[0]["is_nullable"] == "NO"


async def test_teams_name_is_unique(db_engine: object) -> None:
    # 0004: duplicate team name must violate UNIQUE(name); distinct names must coexist.
    async with app_db.SessionFactory() as session:
        await session.execute(text("INSERT INTO teams (name) VALUES ('acme')"))
        await session.commit()

    async with app_db.SessionFactory() as session:
        with_error = False
        try:
            await session.execute(text("INSERT INTO teams (name) VALUES ('acme')"))
            await session.commit()
        except Exception:
            with_error = True
        assert with_error, "same team name twice must violate UNIQUE(name)"

    async with app_db.SessionFactory() as session:
        await session.execute(text("INSERT INTO teams (name) VALUES ('acme-2')"))
        await session.commit()  # distinct name: OK
