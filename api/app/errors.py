"""Canonical error codes and messages — the single source of truth.

This module fixes the exact code + message text of every
rejection an agent can receive. Every message ends in an operational
instruction (actionable errors): it tells the agent what to do next. The services
(``services/memory.py``), the auth middleware (``app/auth.py``) and the MCP
tool layer (``app/mcp_server.py``) import codes and message builders from here
rather than inlining the texts, so the exact wording lives in exactly one
place. ``VERSION_CONFLICT`` additionally carries ``current_version`` in its
payload; that field is set by the caller (``ServiceError.extra``), not here.
"""

from __future__ import annotations

# --- codes -----------------------------------------------------------------

UNAUTHORIZED = "UNAUTHORIZED"
RATE_LIMITED = "RATE_LIMITED"
NOT_FOUND = "NOT_FOUND"
ALREADY_EXISTS = "ALREADY_EXISTS"
ENTRY_TOO_LARGE = "ENTRY_TOO_LARGE"
FRAGMENT_TOO_LARGE = "FRAGMENT_TOO_LARGE"
TOO_MANY_FRAGMENTS = "TOO_MANY_FRAGMENTS"
VERSION_CONFLICT = "VERSION_CONFLICT"
VALIDATION_ERROR = "VALIDATION_ERROR"


# --- static messages (exact text) -----------------------------

UNAUTHORIZED_MESSAGE = (
    "Invalid or missing API key. Pass 'Authorization: Bearer arc_...' — ask your admin for a key."
)

RATE_LIMITED_MESSAGE = "Rate limit exceeded (120 req/min per key). Wait a few seconds and retry."


# --- parametric messages (exact text) -------------------------


def not_found(keyword: str, project: str) -> str:
    return (
        f"Keyword '{keyword}' not found in project '{project}'. "
        "To create it, call memory_upsert with your refined content."
    )


def already_exists(keyword: str) -> str:
    return (
        f"Keyword '{keyword}' already exists. Read it with memory_get, then add "
        "your concept with memory_append (preferred), or compact with "
        "memory_upsert + expected_version."
    )


def entry_too_large(tokens: int) -> str:
    return (
        f"Content is {tokens} tokens; the hard cap is 10000. Split the concept "
        "into multiple keywords."
    )


def soft_cap_warning(tokens: int) -> str:
    """Non-fatal notice on the success payload when an entry is over the 2000
    soft cap but within the 10000 hard cap. The write already succeeded; this
    only nudges the agent to re-refine if it can compress without losing data."""
    return (
        f"Entry is {tokens} tokens, over the 2000 soft cap (accepted). If it "
        "can be compressed without losing information, re-refine ~10% smaller "
        "and memory_upsert again; repeat while it keeps shrinking. Stop when a "
        "pass removes <5%, when you drop under 2000, or when nothing more can "
        "go without losing data — then keep it as-is."
    )


def fragment_too_large(tokens: int) -> str:
    return f"Fragment is {tokens} tokens; the cap is 500. Refine it down to the essential facts."


def too_many_fragments(keyword: str) -> str:
    return (
        f"Keyword '{keyword}' has 10 pending fragments. Compact it first: "
        "memory_get, merge everything into one refined content, then "
        "memory_upsert with expected_version and last_fragment_id."
    )


def version_conflict(expected: int, current: int) -> str:
    return (
        f"Version mismatch: expected {expected}, current is {current}. Someone "
        "compacted meanwhile. Re-read with memory_get, merge again, retry "
        "memory_upsert."
    )


# --- validation messages (VALIDATION_ERROR) -------------


def missing_keywords() -> str:
    """``memory_get`` called with neither ``keywords`` nor its ``keyword`` alias."""
    return (
        'No keyword to read. Pass keywords=["your-keyword"] (a bare string and '
        "the singular keyword= are accepted too), then retry memory_get."
    )


def missing_fragment() -> str:
    """``memory_append`` called with neither ``fragment`` nor its ``content`` alias."""
    return (
        'No fragment to append. Pass fragment="<your refined fragment>" '
        "(content= is accepted as an alias), then retry memory_append."
    )


def pending_fragments() -> str:
    """Compaction (``memory_upsert`` with ``expected_version``) omitted
    ``last_fragment_id`` while fragments are pending: it would orphan them."""
    return (
        "This entry has pending fragments to consume. Call memory_get "
        "first, then pass its last_fragment_id to memory_upsert so the "
        "fragments you merged are consumed atomically."
    )
