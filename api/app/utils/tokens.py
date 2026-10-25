"""Token counting via tiktoken ``cl100k_base``."""

from __future__ import annotations

import tiktoken

from app.paths import configure_cache_env

# tiktoken downloads cl100k_base on first use and caches it in the OS temp dir
# unless TIKTOKEN_CACHE_DIR says otherwise. Pin it under the Archivist root
# before the encoder is ever built (no-op when the deployment sets its own).
configure_cache_env()

_encoder: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    """Load the ``cl100k_base`` encoder once (module-level, lazy)."""
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def count_tokens(text: str) -> int:
    """Return the exact ``cl100k_base`` token count for ``text``."""
    return len(_get_encoder().encode(text))
