"""Remote MCP server: the six memory tools on ``/mcp`` (T1.5).

``build_mcp()`` returns a ``FastMCP(stateless_http=True)`` instance with the six
tools registered; ``create_app`` (``app.main``) mounts its
``streamable_http_app()`` on ``/mcp`` behind the T1.2 Bearer auth middleware and
runs its session manager in the app lifespan. A fresh instance is built per app
because ``StreamableHTTPSessionManager.run()`` is one-shot per instance.

Each tool is a thin adapter over ``services/memory.py`` and ``services/search.py``:
open a session, read the team and owner from the ``current_key`` auth contextvar
(the team id scopes every service call; the team name feeds ``created_by`` /
``updated_by``; the owner name feeds the ``key_owner`` audit field), call the
pure-SQL service, and return the Appendix B JSON contract. Service rejections
(``ServiceError``) and input validation errors are NEVER raised to the agent —
they come back as the Appendix C envelope ``{"error": {"code", "message", ...}}``
so the message itself tells the agent what to do next (rule 7).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, errors
from app.auth import current_key
from app.config import get_settings
from app.embeddings.base import get_embedder as _default_get_embedder
from app.log import get_logger
from app.services import dedup as dedup_svc
from app.services import hybrid as hybrid_svc
from app.services import memory as memory_svc
from app.services import search as search_svc
from app.services.memory import ServiceError

logger = get_logger()


def _get_embedder():
    """Indirection so tests can inject a dependency-free embedder."""
    return _default_get_embedder()


# DNS-rebinding protection guards stdio-launched local servers against browser
# attacks on localhost; Archivist is a remote API already gated by TLS (Caddy) +
# Bearer auth, and the forwarded Host varies per deployment, so it is disabled
# here (see SECURITY.md, T1.5 note).
_TRANSPORT_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)

# Server instructions (T2.1): the refine→append→compaction write protocol, the
# project-slug uniqueness rule and the retrieve pipeline, addressed to the coding
# agent. Every MCP client receives this in the `initialize` response, so the whole
# protocol travels with the connection — no agent-side code required (rule: the
# protocol lives in the instructions + tool descriptions). Kept dense, ≤650 tokens
# (tiktoken cl100k_base), asserted by tests/test_mcp_server.py.
# _INSTRUCTIONS v3 — gap-closure: (a) the pre-write search probe is optional now
# that the server returns near_duplicates on create; (b) correct-on-contradiction
# uses the new age_days field; (c) compaction is "same turn if you can" — deferring
# is safe (every version is kept, the append queue is bounded), so a busy agent
# may defer instead of interrupting its task.
# v3.1 — restores the sweeper mention (removed in 3709f61 when it didn't exist
# yet): the optional archivist-companion `/archivist-companion:sweep` command now
# reconciles compactions a busy agent deferred. Not automatic — a team may not
# have the plugin installed — so in-turn compaction stays the primary path.
_INSTRUCTIONS = """\
Archivist is shared, keyword-based memory for the coding agents on this team. \
Refining content is YOUR job — the server only stores and searches, it runs no \
LLM. Follow this protocol.

PROJECT NAMESPACE (uniqueness rule). Every tool takes a `project` slug; memory is \
shared by the whole team under it. Derive the slug from the git remote: take the \
repository name and normalize it — lowercase, drop the `.git` suffix, slugify. \
`git@github.com:acme/Billing-API.git` → `billing-api`. No remote → normalize the \
root directory name. NEVER invent or abbreviate the slug: teammates who pick \
different names split the namespace and never see each other's memory.

WRITE (save a concept).
1. Extract 1-3 keywords from the concept.
2. Keyword you know exists → refine a dense fragment ≤500 tokens and \
`memory_append` it. Never blocks, never conflicts.
3. New concept → write a self-contained mini-context ≤2000 tokens and \
`memory_upsert` it (omit `expected_version`). If the response lists \
`near_duplicates` and one is the same concept, self-correct now: \
`memory_delete` your new entry and `memory_append` a fragment to the existing \
keyword. Unsure whether a keyword exists? Probe first with `memory_search` \
`intent="write"`.
4. `compaction_due=true` → see COMPACTION.

RETRIEVE. Search BEFORE any non-trivial task and whenever you meet an unknown \
entity (module, tool, error, convention). `memory_search` with `intent="read"`, \
follow `related_keywords`; `memory_get` only the keywords that matter; \
`memory_list` to orient in a new project. Nothing found and the concept is \
cross-cutting → retry with `scope="all"`. Writes always stay in the current \
project. Results carry `age_days` — treat old entries with care, and if a memory \
contradicts what you observe in the code, fix it in the same turn with a \
correction fragment (`memory_append`).

COMPACTION (routine upkeep). When any response shows `compaction_due=true`, \
compact in the same turn if you can — no user permission needed; it is safe, \
every version is kept in history. Steps: `memory_get` the keyword, merge base + \
fragments into one refined content ≤2000 tokens, `memory_upsert` with the \
`expected_version` and `last_fragment_id` you read. `VERSION_CONFLICT` → someone \
compacted first: re-read, retry. Mid-task and cannot stop? Deferring is safe — \
every version is kept and the append queue is bounded — and if the team has the \
archivist-companion plugin, its `/archivist-companion:sweep` command reconciles \
overdue compactions; still, compact in-turn when you can.

CONTENT STYLE. Dense, factual, English, no fluff. Self-contained: readable months \
later without this session's context.

DO NOT SAVE. Secrets or credentials; framework/library trivia; anything \
recoverable from the code in ~10 seconds."""


def _owner() -> str:
    """Owner name from the auth contextvar (the middleware sets it on ``/mcp``)."""
    ctx = current_key.get()
    return ctx.owner_name if ctx is not None else "unknown"


def _team() -> tuple[int, str]:
    """(team_id, team_name) from the auth contextvar; the middleware sets it on /mcp."""
    ctx = current_key.get()
    if ctx is None:
        return (0, "unknown")
    return (ctx.team_id, ctx.team_name)


def _error(code: str, message: str, **extra: Any) -> dict:
    return {"error": {"code": code, "message": message, **extra}}


async def _run(
    tool: str,
    handler: Callable[[AsyncSession], Awaitable[dict]],
    *,
    project: str,
    keyword: str | None = None,
    audit_fields: dict[str, Any] | None = None,
    audit_extra: Callable[[dict], dict[str, Any]] | None = None,
) -> dict:
    """Open a session, run a tool body, map known failures to Appendix C.

    ``ServiceError`` carries a canonical code + message (and structured extras
    such as ``current_version``); a ``ValueError`` is an input-validation
    rejection (e.g. an invalid ``scope`` or a compaction missing
    ``last_fragment_id``). Neither is ever surfaced as a raw MCP exception.

    Emits exactly one JSON audit line per call (T2.2): ``tool``, ``project``,
    ``keyword``, ``key_owner``, ``duration_ms`` and ``outcome`` (``ok`` or the
    rejection's error code). ``outcome`` starts as ``error`` so an unexpected
    exception is still recorded truthfully before it propagates.
    ``audit_fields``, when given, are request-derived fields merged
    unconditionally — recorded even on the error path (e.g. ``intent`` on a
    search, the v1.x #1 read/write tag). ``audit_extra``, when given, derives
    fields from a *successful* result only (e.g. ``result_count`` on a search —
    the T3.4 zero-result-rate metric).
    """
    start = time.perf_counter()
    outcome = "error"
    result: dict | None = None
    try:
        async with db.SessionFactory() as session:
            result = await handler(session)
        outcome = "ok"
        return result
    except ServiceError as exc:
        outcome = exc.code
        return _error(exc.code, exc.message, **exc.extra)
    except ValueError as exc:
        outcome = errors.VALIDATION_ERROR
        return _error(errors.VALIDATION_ERROR, str(exc))
    finally:
        extra: dict[str, Any] = {
            "tool": tool,
            "project": project,
            "keyword": keyword,
            "key_owner": _owner(),
            "duration_ms": round((time.perf_counter() - start) * 1000, 3),
            "outcome": outcome,
        }
        if audit_fields is not None:
            extra.update(audit_fields)
        if audit_extra is not None and result is not None:
            extra.update(audit_extra(result))
        logger.info("tool_call", extra=extra)


def build_mcp() -> FastMCP:
    """Build a FastMCP server with the six tools registered."""
    mcp = FastMCP(
        name="archivist",
        instructions=_INSTRUCTIONS,
        stateless_http=True,
        # Route the endpoint at the mount root so mounting on ``/mcp`` yields the
        # canonical ``/mcp`` URL (not ``/mcp/mcp``).
        streamable_http_path="/",
        transport_security=_TRANSPORT_SECURITY,
    )

    @mcp.tool(
        description=(
            "Search shared memory by exact/prefix keyword and full text over "
            "content AND pending fragments. Returns ranked results (keyword, "
            "excerpt, score, pending_fragments, token_count, age_days) plus "
            "related_keywords to reuse instead of creating duplicates. "
            "scope='project' (default) searches the current project; scope='all' "
            "searches every project (each result carries its own project). Search "
            "BEFORE starting non-trivial work or when you hit an unknown entity. "
            "Pass intent='read' for a genuine retrieval or intent='write' for the "
            "pre-create dedup probe — an audit signal only, it does not affect "
            "results."
        )
    )
    async def memory_search(
        project: str,
        query: str,
        limit: int = 5,
        scope: str = "project",
        intent: str = "unspecified",
    ) -> dict:
        hybrid_on = get_settings().enable_hybrid_search

        async def _do(session: AsyncSession) -> dict:
            team_id, _ = _team()
            if hybrid_on:
                return await hybrid_svc.hybrid_search(
                    session, _get_embedder(), team_id, project, query, limit, scope
                )
            return await search_svc.search_memories(session, team_id, project, query, limit, scope)

        # ``intent`` is observational only: it never touches the query. An
        # unrecognized value is coerced to ``unspecified`` so a typo can never
        # break a real search — an observability field must not break the
        # operation it observes.
        tag = intent if intent in ("read", "write") else "unspecified"
        # Record the intent tag (v1.x #1: de-confounds the read-path zero-result
        # rate), the hit count (T3.4) and the search_mode / vector_rescued flag
        # switch (v1.x #2). vector_rescued is audit-only: popped from the tool
        # payload below so the result contract is unchanged.
        fields: dict[str, Any] = {
            "intent": tag,
            "search_mode": "hybrid" if hybrid_on else "lexical",
        }

        def _extra(r: dict) -> dict[str, Any]:
            out: dict[str, Any] = {"result_count": len(r.get("results", []))}
            if "vector_rescued" in r:
                out["vector_rescued"] = r["vector_rescued"]
            return out

        result = await _run(
            "memory_search", _do, project=project, audit_fields=fields, audit_extra=_extra
        )
        if isinstance(result, dict):
            result.pop("vector_rescued", None)
        return result

    @mcp.tool(
        description=(
            "Read entries by keyword: base content + pending fragments + version "
            "+ last_fragment_id + compaction_due + token_total + age_days + "
            "fragment_count. Unknown keywords "
            "come back in not_found. If compaction_due is true, COMPACT NOW: merge "
            "the base and all fragments into one refined content and call "
            "memory_upsert with the returned expected_version and last_fragment_id."
        )
    )
    async def memory_get(project: str, keywords: list[str]) -> dict:
        async def _do(session: AsyncSession) -> dict:
            team_id, _ = _team()
            return await memory_svc.get_entries(session, team_id, project, keywords)

        return await _run("memory_get", _do, project=project, keyword=",".join(keywords))

    @mcp.tool(
        description=(
            "Append a refined fragment to an EXISTING entry — the default write "
            "path once a keyword exists. Never blocks, never conflicts. When the "
            "response has compaction_due=true the entry crossed the threshold: "
            "read it with memory_get and compact it via memory_upsert. If the "
            "keyword does not exist yet, create it with memory_upsert instead."
        )
    )
    async def memory_append(project: str, keyword: str, fragment: str) -> dict:
        team_id, team_name = _team()

        async def _do(session: AsyncSession) -> dict:
            return await memory_svc.append_fragment(session, team_id, project, keyword, fragment, team_name)

        return await _run("memory_append", _do, project=project, keyword=keyword)

    @mcp.tool(
        description=(
            "Create a new entry, or write a compaction result. New keyword: omit "
            "expected_version and pass the refined content — the entry is created "
            "at version 1. Existing keyword: this is a compaction — pass "
            "expected_version (from your memory_get read) to replace the base "
            "content and bump the version, and pass last_fragment_id to consume "
            "the fragments you merged (required whenever fragments are pending). A "
            "version mismatch returns VERSION_CONFLICT: re-read, merge again, retry. "
            "On create, the response may include near_duplicates (existing keywords "
            "that look like the same concept): if one matches, self-correct — "
            "memory_delete your new entry and memory_append a fragment to the "
            "existing keyword."
        )
    )
    async def memory_upsert(
        project: str,
        keyword: str,
        content: str,
        expected_version: int | None = None,
        last_fragment_id: int | None = None,
    ) -> dict:
        team_id, team_name = _team()

        async def _do(session: AsyncSession) -> dict:
            # Dispatch on expected_version: absent → create (create_entry raises
            # ALREADY_EXISTS if the keyword is already present, i.e. the caller
            # should have compacted); present → compaction (compact_entry raises
            # NOT_FOUND if the keyword is gone).
            if expected_version is None:
                # S2: embed the keyword (~5 ms, local, off the event loop) for
                # near-duplicate detection. Any embedder failure degrades to a
                # plain create — the advisory must never break the write.
                kvec: list[float] | None = None
                try:
                    embedder = _get_embedder()
                    kvec = (await asyncio.to_thread(embedder.embed, [keyword]))[0]
                except Exception:
                    logger.warning("keyword_embed_failed", extra={"tool": "memory_upsert"})
                result = await memory_svc.create_entry(
                    session, team_id, project, keyword, content, team_name, keyword_embedding=kvec
                )
                if kvec is not None:
                    # Advisory-only and runs after create_entry has committed: never
                    # let a lookup failure report an error for a write that already
                    # succeeded — degrade silently, same pattern as the embed above.
                    try:
                        dups = await dedup_svc.find_near_duplicates(
                            session,
                            team_id,
                            project,
                            kvec,
                            exclude_keyword=keyword,
                            threshold=get_settings().near_duplicate_threshold,
                        )
                        if dups:
                            result["near_duplicates"] = dups
                    except Exception:
                        logger.warning("near_dup_lookup_failed", extra={"tool": "memory_upsert"})
                return result
            if (
                last_fragment_id is None
                and await memory_svc.count_pending_fragments(session, team_id, project, keyword) > 0
            ):
                raise ValueError(errors.pending_fragments())
            return await memory_svc.compact_entry(
                session, team_id, project, keyword, content, expected_version, last_fragment_id, team_name
            )

        return await _run("memory_upsert", _do, project=project, keyword=keyword)

    @mcp.tool(
        description=(
            "List every keyword in a project with token_count, pending_fragments, "
            "compaction_due and updated_at — use it to get oriented in a project."
        )
    )
    async def memory_list(project: str) -> dict:
        async def _do(session: AsyncSession) -> dict:
            team_id, _ = _team()
            return await memory_svc.list_entries(session, team_id, project)

        return await _run("memory_list", _do, project=project)

    @mcp.tool(
        description=(
            "Soft-delete an entry (history is preserved). Returns deleted=true, or "
            "NOT_FOUND if the keyword is not present in the project."
        )
    )
    async def memory_delete(project: str, keyword: str) -> dict:
        team_id, team_name = _team()

        async def _do(session: AsyncSession) -> dict:
            return await memory_svc.delete_entry(session, team_id, project, keyword, team_name)

        return await _run("memory_delete", _do, project=project, keyword=keyword)

    return mcp
