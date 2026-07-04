"""Embedder Protocol + provider factory.

The embedder turns text into vectors for the async worker (documents) and the
read path (queries). It is pluggable: the default is a self-hosted local model;
an external HTTP provider is swappable via ``EMBEDDING_PROVIDER=external``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol, runtime_checkable

from app.config import get_settings


@runtime_checkable
class Embedder(Protocol):
    dim: int
    model_id: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector (length ``dim``) per input text, in order."""
        ...


@lru_cache
def get_embedder() -> Embedder:
    """Process-wide embedder selected by settings (cached — models are heavy)."""
    s = get_settings()
    if s.embedding_provider == "external":
        from app.embeddings.external import ExternalEmbedder

        return ExternalEmbedder(model_id=s.embedding_model, dim=s.embedding_dim)
    from app.embeddings.local import LocalEmbedder

    return LocalEmbedder(model_id=s.embedding_model, dim=s.embedding_dim)
