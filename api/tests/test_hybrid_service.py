import threading

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth import create_team
from app.services import memory
from app.services.hybrid import hybrid_search, rrf_fuse
from app.worker import embed_batch
from tests.fakes import FakeEmbedder


class _ThreadRecordingEmbedder(FakeEmbedder):
    """Records the thread ``embed`` runs on, to prove it is off the event loop."""

    def __init__(self) -> None:
        self.embed_thread: int | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_thread = threading.get_ident()
        return super().embed(texts)


def test_rrf_fuse_orders_by_reciprocal_rank():
    # 'b' appears high in both lists → should win.
    a = ["x", "b", "y"]
    c = ["b", "z", "w"]
    fused = rrf_fuse([a, c], k=60)
    assert fused[0][0] == "b"
    assert {k for k, _ in fused} == {"x", "b", "y", "z", "w"}
    scores = [score for _, score in fused]
    assert scores == sorted(scores, reverse=True)


def test_rrf_fuse_single_list_preserves_order():
    fused = rrf_fuse([["a", "b", "c"]], k=60)
    assert [k for k, _ in fused] == ["a", "b", "c"]


@pytest.fixture
async def team_id(db_engine: object) -> int:
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        return await create_team(s, "acme")


async def _seed_and_embed(db_engine, team_id, entries):
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        for kw, content in entries:
            await memory.create_entry(s, team_id, "proj", kw, content, "alice")
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        await embed_batch(s, FakeEmbedder(), batch=100)


@pytest.mark.asyncio
async def test_hybrid_search_returns_contract(db_engine, team_id):
    await _seed_and_embed(
        db_engine, team_id, [("alpha", "the alpha content"), ("beta", "beta content")]
    )
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        out = await hybrid_search(
            s, FakeEmbedder(), team_id, "proj", "alpha", limit=5, scope="project"
        )
    assert "results" in out and "related_keywords" in out and "vector_rescued" in out
    if out["results"]:
        r = out["results"][0]
        assert set(r) >= {
            "project",
            "keyword",
            "score",
            "excerpt",
            "pending_fragments",
            "token_count",
        }


@pytest.mark.asyncio
async def test_hybrid_search_embeds_off_event_loop(db_engine, team_id):
    """Query embedding is CPU-bound (fastembed/ONNX): it must run in a worker
    thread, not block the async read path's event loop."""
    await _seed_and_embed(db_engine, team_id, [("alpha", "the alpha content")])
    rec = _ThreadRecordingEmbedder()
    loop_thread = threading.get_ident()
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        await hybrid_search(s, rec, team_id, "proj", "alpha", limit=5, scope="project")
    assert rec.embed_thread is not None
    assert rec.embed_thread != loop_thread


@pytest.mark.asyncio
async def test_hybrid_search_scope_all(db_engine, team_id):
    await _seed_and_embed(db_engine, team_id, [("alpha", "content one")])
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        out = await hybrid_search(
            s, FakeEmbedder(), team_id, "otherproj", "alpha", limit=5, scope="all"
        )
    # scope='all' can still surface the entry from 'proj'.
    assert isinstance(out["results"], list)


@pytest.mark.asyncio
async def test_hybrid_scope_all_does_not_cross_teams(db_engine):
    """The vector KNN (_vector_knn) must stay team-scoped even under scope='all'.

    Team A's entry is the ONLY row in the whole table with a non-null
    embedding, so an unscoped KNN would unconditionally return it as the
    nearest (only) neighbour. If ``Project.team_id == team_id`` were ever
    dropped from ``_vector_knn``, team B's scope='all' search below would
    surface it — this test would fail.
    """
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        team_a = await create_team(s, "hybrid-team-a")
        team_b = await create_team(s, "hybrid-team-b")

    await _seed_and_embed(
        db_engine, team_a, [("team-a-secret", "team a confidential redis-cache tuning notes")]
    )

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        out = await hybrid_search(
            s, FakeEmbedder(), team_b, "otherproj", "redis-cache tuning", limit=5, scope="all"
        )
    assert all(r["keyword"] != "team-a-secret" for r in out["results"])
    assert all(r["keyword"] != "team-a-secret" for r in out["related_keywords"])


@pytest.mark.asyncio
async def test_hybrid_hits_carry_age_days(db_engine, team_id):
    await _seed_and_embed(db_engine, team_id, [("redis-cache", "caching notes")])
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        out = await hybrid_search(s, FakeEmbedder(), team_id, "proj", "redis-cache")
    assert out["results"] and all("age_days" in r for r in out["results"])


class _BrokenEmbedder(FakeEmbedder):
    """Simulates an embedder that fails at query time (e.g. model missing)."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("model unavailable")


@pytest.mark.asyncio
async def test_hybrid_search_degrades_to_lexical_when_embedder_fails(db_engine, team_id):
    """Query-time embedding failure must not error the whole search: fall back
    to pure lexical results instead (graceful degradation, symmetric with the
    document-side ``embedding IS NOT NULL`` filter in ``_vector_knn``)."""
    await _seed_and_embed(db_engine, team_id, [("redis-cache", "caching notes")])
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        out = await hybrid_search(
            s, _BrokenEmbedder(), team_id, "proj", "redis-cache", limit=5, scope="project"
        )
    assert out["vector_rescued"] is False
    assert any(r["keyword"] == "redis-cache" for r in out["results"])
    assert all("age_days" in r for r in out["results"])
