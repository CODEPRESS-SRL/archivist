"""Admin CLI: team + key lifecycle, incl. painless rotation (same team keeps archive)."""
from __future__ import annotations

from sqlalchemy import select

from app import db as app_db
from app.auth import hash_api_key
from app.models import ApiKey
from scripts import create_api_key as cli  # scripts/ is importable via the sys.path shim in the module


async def test_team_create_then_key_issue(db_engine: object, capsys) -> None:
    team_id = await cli.team_create("acme")
    plaintext = await cli.key_issue(team=str(team_id), label="acme shared")
    async with app_db.SessionFactory() as session:
        row = (await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(plaintext)))).scalar_one()
    assert row.team_id == team_id
    assert row.owner_name == "acme shared"


async def test_rotation_keeps_same_team(db_engine: object) -> None:
    team_id = await cli.team_create("acme")
    first = await cli.key_issue(team="acme", label="v1")   # resolve team by name
    second = await cli.key_issue(team="acme", label="v2")
    async with app_db.SessionFactory() as session:
        rows = (await session.execute(select(ApiKey).where(ApiKey.team_id == team_id))).scalars().all()
    assert {r.owner_name for r in rows} == {"v1", "v2"}
    assert first != second


async def test_key_rotate_revoke_old_revokes_old_key_and_keeps_new_active(db_engine: object) -> None:
    """--revoke-old revokes the pre-rotation key(s) while the freshly-issued key
    stays active, and both keys remain on the same team (painless rotation)."""
    team_id = await cli.team_create("acme")
    old_plaintext = await cli.key_issue(team="acme", label="v1")
    async with app_db.SessionFactory() as session:
        old_row = (
            await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(old_plaintext)))
        ).scalar_one()
    assert old_row.revoked is False

    new_plaintext = await cli.key_rotate(team="acme", label="v2", revoke_old=True)
    assert new_plaintext != old_plaintext

    async with app_db.SessionFactory() as session:
        old_row = (
            await session.execute(select(ApiKey).where(ApiKey.id == old_row.id))
        ).scalar_one()
        new_row = (
            await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(new_plaintext)))
        ).scalar_one()

    assert old_row.revoked is True
    assert new_row.revoked is False
    assert old_row.team_id == team_id
    assert new_row.team_id == team_id
