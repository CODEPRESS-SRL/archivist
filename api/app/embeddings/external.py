"""External HTTP embedder (thin stub for the prototype).

Kept behind the same Protocol so a hosted provider can be swapped in via
``EMBEDDING_PROVIDER=external`` without touching the worker or the read path.
Wiring a concrete API (key handling, batching, retries) is out of scope for the
prototype — see the design's "Out of scope". Constructing it fails fast, so an
``EMBEDDING_PROVIDER=external`` misconfiguration surfaces at startup (worker
boot / first search wiring) rather than as a runtime 500 on the embed call.
"""

from __future__ import annotations


class ExternalEmbedder:
    def __init__(self, model_id: str, dim: int) -> None:
        raise NotImplementedError(
            "ExternalEmbedder is a prototype stub; set EMBEDDING_PROVIDER=local "
            "or implement a concrete embedding API client."
        )
