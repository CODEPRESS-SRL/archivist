"""Server-side keyword normalization (lowercase, trim, slug).

Implemented in T1.1 (Keyword and token utilities).
"""

from __future__ import annotations

import re

_MAX_LENGTH = 64
_SEPARATORS = re.compile(r"[\s_]+")
_ALLOWED = re.compile(r"[^a-z0-9-]+")
_MULTI_DASH = re.compile(r"-+")


def normalize_keyword(raw: str) -> str:
    """Normalize ``raw`` to the binding keyword format.

    Lowercases, trims, maps spaces/underscores to ``-``, drops any
    character outside ``[a-z0-9-]``, collapses repeated dashes, strips
    dashes at both ends, and truncates to 64 characters.

    Raises:
        ValueError: if the normalized result is empty.
    """
    value = raw.lower()
    value = _SEPARATORS.sub("-", value)
    value = _ALLOWED.sub("", value)
    value = _MULTI_DASH.sub("-", value)
    value = value.strip("-")
    if len(value) > _MAX_LENGTH:
        value = value[:_MAX_LENGTH].strip("-")

    if not value:
        raise ValueError(f"keyword {raw!r} normalizes to an empty string")
    return value
