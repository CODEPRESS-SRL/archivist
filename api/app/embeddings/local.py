"""Self-hosted embedder using fastembed (ONNX runtime; no torch, CPU-friendly)."""

from __future__ import annotations

from fastembed import TextEmbedding


class LocalEmbedder:
    def __init__(self, model_id: str, dim: int) -> None:
        self.model_id = model_id
        self.dim = dim
        self._model = TextEmbedding(model_name=model_id)

    def embed(self, texts: list[str]) -> list[list[float]]:
        # fastembed yields numpy arrays; normalize to plain lists of floats.
        return [vec.tolist() for vec in self._model.embed(texts)]
