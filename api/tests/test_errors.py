"""T1.6 — ``app.errors`` is the single source of the Appendix C canonical texts.

Two DoD guarantees are checked here, per code:
1. the rendered message equals its canonical Appendix C text exactly, and
2. every message carries an operational instruction (rule 7): it names the
   next action the agent should take.

The canonical texts are duplicated in this test as an independent oracle —
tests are allowed to; the DoD's "no error string duplicated outside errors.py"
grep is over the ``app/`` sources only, not the tests.
"""

from __future__ import annotations

import re

import pytest

from app import errors

# (code, rendered message, canonical Appendix C text)
_CASES: list[tuple[str, str, str]] = [
    (
        errors.UNAUTHORIZED,
        errors.UNAUTHORIZED_MESSAGE,
        "Invalid or missing API key. Pass 'Authorization: Bearer arc_...' — "
        "ask your admin for a key.",
    ),
    (
        errors.RATE_LIMITED,
        errors.RATE_LIMITED_MESSAGE,
        "Rate limit exceeded (120 req/min per key). Wait a few seconds and retry.",
    ),
    (
        errors.NOT_FOUND,
        errors.not_found("redis", "billing-api"),
        "Keyword 'redis' not found in project 'billing-api'. To create it, "
        "call memory_upsert with your refined content.",
    ),
    (
        errors.ALREADY_EXISTS,
        errors.already_exists("redis"),
        "Keyword 'redis' already exists. Read it with memory_get, then add your "
        "concept with memory_append (preferred), or compact with memory_upsert "
        "+ expected_version.",
    ),
    (
        errors.ENTRY_TOO_LARGE,
        errors.entry_too_large(2500),
        "Content is 2500 tokens; the cap is 2000. Compress it further, or split "
        "the concept into two keywords.",
    ),
    (
        errors.FRAGMENT_TOO_LARGE,
        errors.fragment_too_large(600),
        "Fragment is 600 tokens; the cap is 500. Refine it down to the essential facts.",
    ),
    (
        errors.TOO_MANY_FRAGMENTS,
        errors.too_many_fragments("redis"),
        "Keyword 'redis' has 10 pending fragments. Compact it first: memory_get, "
        "merge everything into one refined content, then memory_upsert with "
        "expected_version and last_fragment_id.",
    ),
    (
        errors.VERSION_CONFLICT,
        errors.version_conflict(3, 5),
        "Version mismatch: expected 3, current is 5. Someone compacted meanwhile. "
        "Re-read with memory_get, merge again, retry memory_upsert.",
    ),
]

_IDS = [case[0] for case in _CASES]

# An imperative next action every rejection must name (rule 7).
_INSTRUCTION = re.compile(
    r"\b(call|read|compact|compress|split|refine|re-read|pass|wait|ask|retry|merge)\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize(("code", "message", "canonical"), _CASES, ids=_IDS)
def test_message_matches_appendix_c(code: str, message: str, canonical: str) -> None:
    assert message == canonical


@pytest.mark.parametrize(("code", "message", "canonical"), _CASES, ids=_IDS)
def test_message_carries_operational_instruction(code: str, message: str, canonical: str) -> None:
    assert _INSTRUCTION.search(message), message


def test_validation_pending_fragments_has_instruction() -> None:
    # VALIDATION_ERROR is not in Appendix C, but its one canonical message must
    # still tell the agent exactly how to recover.
    msg = errors.pending_fragments()
    assert "memory_get" in msg
    assert "last_fragment_id" in msg
    assert _INSTRUCTION.search(msg), msg
