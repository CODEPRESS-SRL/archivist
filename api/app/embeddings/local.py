"""Self-hosted embedder using fastembed (ONNX runtime; no torch, CPU-friendly)."""

from __future__ import annotations

from fastembed import TextEmbedding

from app.paths import configure_cache_env

# fastembed downloads a ~130 MB ONNX model on first use and caches it in the OS
# temp dir unless FASTEMBED_CACHE_PATH says otherwise. Pin it under the
# Archivist root (the Docker image sets its own, which wins).
configure_cache_env()


class LocalEmbedder:
    def __init__(self, model_id: str, dim: int) -> None:
        self.model_id = model_id
        self.dim = dim
        self._model = TextEmbedding(model_name=model_id)

    def embed(self, texts: list[str]) -> list[list[float]]:
        # fastembed yields numpy arrays; normalize to plain lists of floats.
        return [vec.tolist() for vec in self._model.embed(texts)]
