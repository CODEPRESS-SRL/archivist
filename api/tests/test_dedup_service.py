"""find_near_duplicates: cosine neighbours of a keyword vector, project-scoped."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth import create_team
from app.services.dedup import find_near_duplicates
from app.services.memory import create_entry

_VEC = [1.0] + [0.0] * 383  # unit vector
_FAR = [0.0, 1.0] + [0.0] * 382  # orthogonal → similarity 0.0


@pytest.mark.asyncio
async def test_finds_identical_keyword_vector(db_engine):
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        team_id = await create_team(s, "acme")
        await create_entry(
            s, team_id, "proj", "dev-server", "how to run it", "t", keyword_embedding=_VEC
        )
        hits = await find_near_duplicates(
            s, team_id, "proj", _VEC, exclude_keyword="other", threshold=0.80
        )
    assert hits and hits[0]["keyword"] == "dev-server"
    assert hits[0]["similarity"] >= 0.99
    assert hits[0]["preview"].startswith("how to run it")


@pytest.mark.asyncio
async def test_excludes_self_and_respects_threshold(db_engine):
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        team_id = await create_team(s, "acme")
        await create_entry(s, team_id, "proj", "dev-server", "x", "t", keyword_embedding=_VEC)
        # Self exclusion: the just-created keyword must not report itself.
        assert (
            await find_near_duplicates(
                s, team_id, "proj", _VEC, exclude_keyword="dev-server", threshold=0.80
            )
            == []
        )
        # An orthogonal vector matches nothing at the threshold.
        assert (
            await find_near_duplicates(
                s, team_id, "proj", _FAR, exclude_keyword="other", threshold=0.80
            )
            == []
        )


@pytest.mark.asyncio
async def test_scoped_to_team_and_project(db_engine):
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        team_id = await create_team(s, "acme")
        other_team_id = await create_team(s, "rivals")
        await create_entry(
            s, other_team_id, "proj", "dev-server", "foreign", "t", keyword_embedding=_VEC
        )
        await create_entry(
            s, team_id, "elsewhere", "dev-server", "other project", "t", keyword_embedding=_VEC
        )
        # Same team+project only: nothing from the other team or the other project.
        assert (
            await find_near_duplicates(
                s, team_id, "proj", _VEC, exclude_keyword="x", threshold=0.80
            )
            == []
        )
