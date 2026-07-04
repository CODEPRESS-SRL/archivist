"""Token counting via tiktoken ``cl100k_base``.

Implemented in T1.1 (Keyword and token utilities).
"""

from __future__ import annotations

import tiktoken

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
