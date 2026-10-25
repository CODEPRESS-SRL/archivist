"""Test doubles reused across worker/hybrid tests."""

from __future__ import annotations

import hashlib


class FakeEmbedder:
    """Deterministic, dependency-free Embedder for tests.

    Maps text → a stable unit-ish vector derived from its SHA-256, so equal
    text yields equal vectors and different text yields different vectors,
    without loading any model.
    """

    dim = 384
    model_id = "fake-embedder"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            # Tile the 32-byte digest up to `dim` floats in [-1, 1].
            vec = [((h[i % len(h)] / 255.0) * 2.0 - 1.0) for i in range(self.dim)]
            out.append(vec)
        return out
