import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth import create_team
from app.services import memory
from app.worker import embed_batch, embed_keyword_batch
from tests.fakes import FakeEmbedder


@pytest.mark.asyncio
async def test_embed_batch_fills_vector_and_clears_flag(db_engine):
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        team_id = await create_team(s, "acme")
        await memory.create_entry(s, team_id, "proj", "kw", "some content", "alice")

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        n = await embed_batch(s, FakeEmbedder(), batch=32)
    assert n == 1

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        row = (
            await s.execute(
                text(
                    "SELECT embedding_stale, embedding_model, embedding IS NOT NULL "
                    "FROM memories WHERE keyword='kw'"
                )
            )
        ).one()
        assert row[0] is False
        assert row[1] == "fake-embedder"
        assert row[2] is True


@pytest.mark.asyncio
async def test_embed_batch_skips_deleted_and_fresh(db_engine):
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        team_id = await create_team(s, "acme")
        await memory.create_entry(s, team_id, "proj", "kw1", "c1", "alice")
        await memory.create_entry(s, team_id, "proj", "kw2", "c2", "alice")
        await memory.delete_entry(s, team_id, "proj", "kw2", "alice")  # soft-deleted → skip
        await s.execute(text("UPDATE memories SET embedding_stale=false WHERE keyword='kw1'"))
        await s.commit()
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        n = await embed_batch(s, FakeEmbedder(), batch=32)
    assert n == 0  # kw1 not stale, kw2 deleted


@pytest.mark.asyncio
async def test_embed_batch_crash_before_update_leaves_flag(db_engine):
    class Boom(FakeEmbedder):
        def embed(self, texts):
            raise RuntimeError("embed failed")

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        team_id = await create_team(s, "acme")
        await memory.create_entry(s, team_id, "proj", "kw", "content", "alice")
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        with pytest.raises(RuntimeError):
            await embed_batch(s, Boom(), batch=32)
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        stale = (
            await s.execute(text("SELECT embedding_stale FROM memories WHERE keyword='kw'"))
        ).scalar_one()
        assert stale is True  # retried next cycle


@pytest.mark.asyncio
async def test_embed_keyword_batch_fills_null_rows(db_engine):
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        team_id = await create_team(s, "acme")
        await memory.create_entry(s, team_id, "proj", "alpha-service", "content", "alice")

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        n = await embed_keyword_batch(s, FakeEmbedder(), batch=32)
    assert n == 1

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        filled = (
            await s.execute(
                text(
                    "SELECT keyword_embedding IS NOT NULL FROM memories WHERE keyword='alpha-service'"
                )
            )
        ).scalar_one()
    assert filled is True


@pytest.mark.asyncio
async def test_embed_keyword_batch_noop_when_all_filled(db_engine):
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        team_id = await create_team(s, "acme")
        await memory.create_entry(s, team_id, "proj", "alpha-service", "content", "alice")

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        assert await embed_keyword_batch(s, FakeEmbedder(), batch=32) == 1
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        assert await embed_keyword_batch(s, FakeEmbedder(), batch=32) == 0
