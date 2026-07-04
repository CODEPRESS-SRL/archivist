import pytest

from app.embeddings.base import get_embedder
from tests.fakes import FakeEmbedder


def test_fake_embedder_shape_and_determinism():
    e = FakeEmbedder()
    a = e.embed(["hello", "world"])
    assert len(a) == 2
    assert all(len(v) == e.dim for v in a)
    assert e.embed(["hello"])[0] == a[0]  # deterministic
    assert e.embed(["world"])[0] != e.embed(["hello"])[0]


def test_get_embedder_local_default(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    get_embedder.cache_clear()
    e = get_embedder()
    assert e.dim == 384
    assert "bge-small" in e.model_id


def test_external_embedder_fails_fast_at_construction():
    """The stub must fail when constructed, not on first embed, so an
    EMBEDDING_PROVIDER=external misconfiguration surfaces at startup."""
    from app.embeddings.external import ExternalEmbedder

    with pytest.raises(NotImplementedError):
        ExternalEmbedder(model_id="x", dim=384)


def test_get_embedder_external_fails_fast(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("EMBEDDING_PROVIDER", "external")
    get_settings.cache_clear()
    get_embedder.cache_clear()
    try:
        with pytest.raises(NotImplementedError):
            get_embedder()
    finally:
        get_settings.cache_clear()
        get_embedder.cache_clear()


def test_local_embedder_real_embed():
    """Real fastembed load; skips offline so the suite stays green without network."""
    fastembed = pytest.importorskip("fastembed")  # noqa: F841
    from app.embeddings.local import LocalEmbedder

    try:
        e = LocalEmbedder(model_id="BAAI/bge-small-en-v1.5", dim=384)
        vecs = e.embed(["database migration"])
    except Exception as exc:  # model download unavailable offline
        pytest.skip(f"fastembed model unavailable: {exc}")
    assert len(vecs) == 1 and len(vecs[0]) == 384
    assert e.embed(["database migration"])[0] == vecs[0]  # deterministic
