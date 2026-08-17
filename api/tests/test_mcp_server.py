"""End-to-end tests for the MCP server and its seven tools (T1.5).

These drive the real MCP SDK streamable-HTTP client against the ASGI app
in-process (the client runs on an httpx ``ASGITransport``), so the auth
middleware, the mounted FastMCP endpoint and the service layer are all exercised
together. A real API key is created in the container DB and passed as the Bearer
token, so the ``updated_by`` / ``created_by`` team attribution flows end to end.

Covers the DoD: ``tools/list`` shows the seven tools; the
search→upsert(create)→append→get→upsert(compact) cycle; cross-project
``scope="all"``; a call without Bearer → HTTP 401 (not an MCP error); a tool call
on a new project creates it in ``projects``; every service rejection comes back
as the Appendix C envelope (parametric over the codes).
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from httpx import ASGITransport
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select

from app import db as app_db
from app.auth import create_api_key, create_team
from app.log import JsonFormatter
from app.main import create_app
from app.models import Memory, MemoryFragment, Project
from app.utils.tokens import count_tokens

_BASE = "http://arc.test"
_MCP_URL = f"{_BASE}/mcp"

# Oversized payloads for the token-cap errors (cl100k_base ≈ one token per word).
_HUGE_CONTENT = "alpha " * 15000  # > 10000 tokens (hard cap)
_HUGE_FRAGMENT = "beta " * 800  # > 500 tokens


async def _new_key(owner: str = "enrico", team: str = "acme") -> str:
    async with app_db.SessionFactory() as session:
        team_id = await create_team(session, team)
        return await create_api_key(session, owner, team_id)


@contextlib.asynccontextmanager
async def _client_session(app: object, token: str | None) -> AsyncIterator[ClientSession]:
    """Yield an initialized MCP ClientSession bound to the app (lifespan running).

    The MCP client speaks to the in-process ASGI app through an httpx client on
    an ``ASGITransport``; the Bearer token (if any) rides on that client, so the
    T1.2 auth middleware sees it on every request.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http_client = httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url=_BASE,
        follow_redirects=True,
        headers=headers,
    )
    async with (
        app.router.lifespan_context(app),
        http_client,
        streamable_http_client(_MCP_URL, http_client=http_client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def _payload(result: object) -> dict:
    """The JSON dict a tool returned (its Appendix B / Appendix C contract)."""
    assert result.content, "tool returned no content"
    return json.loads(result.content[0].text)


async def _call(session: ClientSession, tool: str, **args: object) -> dict:
    return _payload(await session.call_tool(tool, args))


# --- tools/list ------------------------------------------------------------

_EXPECTED_TOOLS = {
    "memory_search",
    "memory_get",
    "memory_append",
    "memory_upsert",
    "memory_list",
    "memory_delete",
    "memory_history",
}


async def test_tools_list_shows_the_seven_tools(db_engine: object) -> None:
    app = create_app()
    token = await _new_key()
    async with _client_session(app, token) as session:
        tools = await session.list_tools()
    assert {t.name for t in tools.tools} == _EXPECTED_TOOLS


# --- the full write/read cycle ---------------------------------------------


async def test_full_cycle_search_upsert_append_get_compact(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hybrid search is ON by default; inject the dependency-free FakeEmbedder
    # via the mcp_server indirection so this contract test never loads a real
    # embedding model.
    from app import mcp_server
    from tests.fakes import FakeEmbedder

    monkeypatch.setattr(mcp_server, "_get_embedder", lambda: FakeEmbedder())

    app = create_app()
    token = await _new_key("enrico")
    project, keyword = "archivist", "redis-cache"

    async with _client_session(app, token) as session:
        # search on an empty project → no results, no error
        first = await _call(session, "memory_search", project=project, query=keyword)
        assert first == {"results": [], "related_keywords": []}

        # upsert (create) → version 1
        created = await _call(
            session, "memory_upsert", project=project, keyword=keyword, content="base content"
        )
        assert created == {
            "keyword": keyword,
            "created": True,
            "version": 1,
            "token_count": created["token_count"],
        }

        # three appends → not yet compaction_due (threshold is > 3)
        for i in range(3):
            appended = await _call(
                session, "memory_append", project=project, keyword=keyword, fragment=f"frag {i}"
            )
            assert appended["pending_fragments"] == i + 1
            assert appended["compaction_due"] is False

        got = await _call(session, "memory_get", project=project, keywords=[keyword])
        entry = got["entries"][0]
        assert entry["version"] == 1
        assert entry["compaction_due"] is False
        assert len(entry["fragments"]) == 3

        # fourth append → crosses the threshold
        fourth = await _call(
            session, "memory_append", project=project, keyword=keyword, fragment="frag 3"
        )
        assert fourth["pending_fragments"] == 4
        assert fourth["compaction_due"] is True

        got = await _call(session, "memory_get", project=project, keywords=[keyword])
        entry = got["entries"][0]
        assert entry["compaction_due"] is True
        last_fragment_id = entry["last_fragment_id"]

        # upsert (compact) consuming the four fragments → version 2
        compacted = await _call(
            session,
            "memory_upsert",
            project=project,
            keyword=keyword,
            content="merged content",
            expected_version=1,
            last_fragment_id=last_fragment_id,
        )
        assert compacted["created"] is False
        assert compacted["version"] == 2
        assert compacted["fragments_consumed"] == 4

        # get again → zero fragments, bumped version, new content
        final = await _call(session, "memory_get", project=project, keywords=[keyword])
        final_entry = final["entries"][0]
        assert final_entry["fragments"] == []
        assert final_entry["version"] == 2
        assert final_entry["content"] == "merged content"

        # the entry is now searchable
        found = await _call(session, "memory_search", project=project, query=keyword)
        assert found["results"][0]["keyword"] == keyword
        assert found["results"][0]["project"] == project


# --- memory_get accepts a single keyword or a list ------------------------


async def test_memory_get_accepts_a_bare_string_keyword(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Agents frequently pass a single keyword as a scalar instead of a
    # one-element list. memory_get must accept ``keywords="k"`` as well as
    # ``keywords=["k"]`` so the common shape is never a malformed call.
    from app import mcp_server
    from tests.fakes import FakeEmbedder

    monkeypatch.setattr(mcp_server, "_get_embedder", lambda: FakeEmbedder())

    app = create_app()
    token = await _new_key()
    project, keyword = "archivist", "design-system"

    async with _client_session(app, token) as session:
        await _call(session, "memory_upsert", project=project, keyword=keyword, content="base")

        # bare string ...
        by_string = await _call(session, "memory_get", project=project, keywords=keyword)
        # ... and one-element list resolve to the same entry
        by_list = await _call(session, "memory_get", project=project, keywords=[keyword])

        assert by_string["entries"][0]["keyword"] == keyword
        assert by_string["not_found"] == []
        assert by_string == by_list


async def test_memory_get_accepts_keyword_as_an_alias_for_keywords(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every other tool names the parameter ``keyword`` (singular), so agents
    # routinely call memory_get with ``keyword=`` too. Accept it as an alias:
    # a wrong-name call used to fail as a raw pydantic validation error.
    from app import mcp_server
    from tests.fakes import FakeEmbedder

    monkeypatch.setattr(mcp_server, "_get_embedder", lambda: FakeEmbedder())

    app = create_app()
    token = await _new_key()
    project, keyword = "archivist", "design-system"

    async with _client_session(app, token) as session:
        await _call(session, "memory_upsert", project=project, keyword=keyword, content="base")

        by_alias = await _call(session, "memory_get", project=project, keyword=keyword)
        by_canonical = await _call(session, "memory_get", project=project, keywords=[keyword])

        assert by_alias == by_canonical
        assert by_alias["entries"][0]["keyword"] == keyword


async def test_memory_get_without_any_keyword_is_a_validation_error(db_engine: object) -> None:
    app = create_app()
    token = await _new_key()

    async with _client_session(app, token) as session:
        result = await _call(session, "memory_get", project="archivist")

    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert "keywords" in result["error"]["message"]


async def test_memory_get_with_an_explicit_empty_list_still_reads_nothing(
    db_engine: object,
) -> None:
    # Accepting the alias must not turn an explicit empty list — which has
    # always meant "nothing to read" — into a rejection.
    app = create_app()
    token = await _new_key()

    async with _client_session(app, token) as session:
        result = await _call(session, "memory_get", project="archivist", keywords=[])

    assert result == {"entries": [], "not_found": []}


# --- memory_append accepts `content` as an alias for `fragment` ------------


async def test_memory_append_accepts_content_as_an_alias_for_fragment(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # memory_upsert names its payload ``content``, so agents carry that name
    # over to memory_append — by far the most common malformed call in the
    # field. Accept it as an alias for ``fragment``.
    from app import mcp_server
    from tests.fakes import FakeEmbedder

    monkeypatch.setattr(mcp_server, "_get_embedder", lambda: FakeEmbedder())

    app = create_app()
    token = await _new_key()
    project, keyword = "archivist", "design-system"

    async with _client_session(app, token) as session:
        await _call(session, "memory_upsert", project=project, keyword=keyword, content="base")

        appended = await _call(
            session, "memory_append", project=project, keyword=keyword, content="via alias"
        )
        assert appended["fragment_id"] > 0

        got = await _call(session, "memory_get", project=project, keywords=[keyword])

    assert [f["content"] for f in got["entries"][0]["fragments"]] == ["via alias"]


async def test_memory_append_without_fragment_or_content_is_a_validation_error(
    db_engine: object,
) -> None:
    app = create_app()
    token = await _new_key()

    async with _client_session(app, token) as session:
        result = await _call(session, "memory_append", project="archivist", keyword="design-system")

    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert "fragment" in result["error"]["message"]


# --- cross-project scope ---------------------------------------------------


async def test_search_scope_all_finds_another_projects_entry(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hybrid search is ON by default; inject FakeEmbedder so this contract test
    # never loads a real embedding model.
    from app import mcp_server
    from tests.fakes import FakeEmbedder

    monkeypatch.setattr(mcp_server, "_get_embedder", lambda: FakeEmbedder())

    app = create_app()
    token = await _new_key()

    async with _client_session(app, token) as session:
        await _call(
            session, "memory_upsert", project="proj-a", keyword="shared-topic", content="in A"
        )

        # scope="project" from proj-b sees nothing
        scoped = await _call(session, "memory_search", project="proj-b", query="shared-topic")
        assert scoped["results"] == []

        # scope="all" from proj-b finds proj-a's entry, carrying its project slug
        cross = await _call(
            session, "memory_search", project="proj-b", query="shared-topic", scope="all"
        )
        assert len(cross["results"]) == 1
        assert cross["results"][0]["project"] == "proj-a"
        assert cross["results"][0]["keyword"] == "shared-topic"


# --- auth boundary ---------------------------------------------------------


async def test_call_without_bearer_is_http_401_not_an_mcp_error(db_engine: object) -> None:
    app = create_app()
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client,
    ):
        response = await client.post(
            "/mcp",
            json=initialize,
            headers={"Accept": "application/json, text/event-stream"},
        )
    # Rejected by the auth middleware at the HTTP layer, before any MCP handling.
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


# --- project auto-creation -------------------------------------------------


async def test_tool_call_on_new_project_creates_it(db_engine: object) -> None:
    app = create_app()
    token = await _new_key()
    async with _client_session(app, token) as session:
        await _call(
            session, "memory_upsert", project="Brand New Repo", keyword="hello", content="hi"
        )

    async with app_db.SessionFactory() as db_session:
        slugs = (await db_session.execute(select(Project.slug))).scalars().all()
    # The project slug is normalized server-side.
    assert "brand-new-repo" in slugs


# --- team attribution -------------------------------------------------------


async def test_team_from_auth_contextvar_is_persisted(db_engine: object) -> None:
    app = create_app()
    token = await _new_key("alice", team="acme-corp")
    async with _client_session(app, token) as session:
        await _call(session, "memory_upsert", project="p", keyword="kw", content="c")
        await _call(session, "memory_append", project="p", keyword="kw", fragment="f")

    async with app_db.SessionFactory() as db_session:
        updated_by = (await db_session.execute(select(Memory.updated_by))).scalar_one()
        created_by = (await db_session.execute(select(MemoryFragment.created_by))).scalar_one()
    assert updated_by == "acme-corp"
    assert created_by == "acme-corp"


# --- delete + list ---------------------------------------------------------


async def test_list_and_delete(db_engine: object) -> None:
    app = create_app()
    token = await _new_key()
    async with _client_session(app, token) as session:
        await _call(session, "memory_upsert", project="p", keyword="kw", content="c")

        listing = await _call(session, "memory_list", project="p")
        assert listing["project"] == "p"
        assert [e["keyword"] for e in listing["entries"]] == ["kw"]

        deleted = await _call(session, "memory_delete", project="p", keyword="kw")
        assert deleted == {"keyword": "kw", "deleted": True}

        after = await _call(session, "memory_list", project="p")
        assert after["entries"] == []


# --- Appendix C error envelope (parametric over the codes) -----------------


async def _provoke(session: ClientSession, code: str) -> dict:
    """Drive each canonical error condition and return the tool's payload."""
    if code == "NOT_FOUND":
        return await _call(session, "memory_append", project="p", keyword="ghost", fragment="f")
    if code == "ALREADY_EXISTS":
        await _call(session, "memory_upsert", project="p", keyword="dup", content="c")
        return await _call(session, "memory_upsert", project="p", keyword="dup", content="c2")
    if code == "ENTRY_TOO_LARGE":
        return await _call(
            session, "memory_upsert", project="p", keyword="big", content=_HUGE_CONTENT
        )
    if code == "FRAGMENT_TOO_LARGE":
        await _call(session, "memory_upsert", project="p", keyword="host", content="c")
        return await _call(
            session, "memory_append", project="p", keyword="host", fragment=_HUGE_FRAGMENT
        )
    if code == "TOO_MANY_FRAGMENTS":
        await _call(session, "memory_upsert", project="p", keyword="full", content="c")
        for i in range(10):
            await _call(session, "memory_append", project="p", keyword="full", fragment=f"f{i}")
        return await _call(session, "memory_append", project="p", keyword="full", fragment="over")
    if code == "VERSION_CONFLICT":
        await _call(session, "memory_upsert", project="p", keyword="ver", content="c")
        return await _call(
            session,
            "memory_upsert",
            project="p",
            keyword="ver",
            content="c2",
            expected_version=99,
        )
    raise AssertionError(code)


@pytest.mark.parametrize(
    "code",
    [
        "NOT_FOUND",
        "ALREADY_EXISTS",
        "ENTRY_TOO_LARGE",
        "FRAGMENT_TOO_LARGE",
        "TOO_MANY_FRAGMENTS",
        "VERSION_CONFLICT",
    ],
)
async def test_error_responses_match_appendix_c(db_engine: object, code: str) -> None:
    app = create_app()
    token = await _new_key()
    async with _client_session(app, token) as session:
        payload = await _provoke(session, code)

    assert set(payload) == {"error"}
    error = payload["error"]
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    if code == "VERSION_CONFLICT":
        assert error["current_version"] == 1


async def test_version_conflict_reports_current_version(db_engine: object) -> None:
    app = create_app()
    token = await _new_key()
    async with _client_session(app, token) as session:
        payload = await _provoke(session, "VERSION_CONFLICT")
    assert payload["error"]["current_version"] == 1


# --- input validation (never a raw MCP exception) --------------------------


async def test_invalid_scope_returns_validation_error(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _get_embedder() is evaluated as a call argument before hybrid_search's
    # internal scope check runs, so even this validation-error test would
    # construct a real embedder under the hybrid-ON default — inject FakeEmbedder.
    from app import mcp_server
    from tests.fakes import FakeEmbedder

    monkeypatch.setattr(mcp_server, "_get_embedder", lambda: FakeEmbedder())

    app = create_app()
    token = await _new_key()
    async with _client_session(app, token) as session:
        payload = await _call(session, "memory_search", project="p", query="x", scope="sideways")
    assert payload["error"]["code"] == "VALIDATION_ERROR"


async def test_compact_without_last_fragment_id_is_rejected(db_engine: object) -> None:
    app = create_app()
    token = await _new_key()
    async with _client_session(app, token) as session:
        await _call(session, "memory_upsert", project="p", keyword="kw", content="c")
        await _call(session, "memory_append", project="p", keyword="kw", fragment="f")
        # Compacting (expected_version set) while fragments are pending but
        # last_fragment_id omitted must be rejected, not silently orphan them.
        payload = await _call(
            session,
            "memory_upsert",
            project="p",
            keyword="kw",
            content="merged",
            expected_version=1,
        )
    assert payload["error"]["code"] == "VALIDATION_ERROR"


async def test_memory_upsert_surfaces_soft_cap_warning(db_engine: object) -> None:
    app = create_app()
    token = await _new_key()
    over_soft = "alpha " * 3000  # > 2000 soft cap, < 10000 hard cap
    async with _client_session(app, token) as session:
        result = await _call(
            session, "memory_upsert", project="p", keyword="verbose", content=over_soft
        )
    assert result["soft_cap_exceeded"] is True
    assert "warning" in result and "2000" in result["warning"]
    assert "error" not in result  # it was accepted, not rejected


# --- server instructions (the write/retrieve protocol, T2.1) ---------------


async def _initialize_result(app: object, token: str) -> object:
    """Open an MCP client just far enough to capture the ``initialize`` result."""
    http_client = httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url=_BASE,
        follow_redirects=True,
        headers={"Authorization": f"Bearer {token}"},
    )
    async with (
        app.router.lifespan_context(app),
        http_client,
        streamable_http_client(_MCP_URL, http_client=http_client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        return await session.initialize()


async def test_instructions_exposed_in_initialize(db_engine: object) -> None:
    app = create_app()
    token = await _new_key()
    result = await _initialize_result(app, token)

    instructions = result.instructions
    assert instructions, "server exposed no instructions in the initialize response"

    # ≤ 650 tokens (tiktoken cl100k_base). Raised from the T2.1 cap of 600 to
    # fit the read/write intent clauses added for the v1.x #1 metric fix.
    assert count_tokens(instructions) <= 650

    # The write protocol: append vs upsert, the compaction trigger + lock fields.
    for token_phrase in (
        "memory_search",
        "memory_append",
        "memory_upsert",
        "compaction_due",
        "expected_version",
        "last_fragment_id",
    ):
        assert token_phrase in instructions

    # The project-slug uniqueness rule, with its worked example.
    assert "git remote" in instructions
    assert "billing-api" in instructions

    # The cross-project retrieve fallback.
    assert 'scope="all"' in instructions

    # The read/write intent tag on the search probe (v1.x #1: de-confounds the
    # read-path zero-result rate). Both paths are tagged.
    assert 'intent="write"' in instructions
    assert 'intent="read"' in instructions

    # v3: server-side dedup advisory + freshness + softened compaction.
    # v3.1: sweep reference restored (archivist-companion's /archivist-companion:sweep command).
    for phrase in ("near_duplicates", "age_days", "sweep"):
        assert phrase in instructions


# --- audit logging (one JSON line per tool call, T2.2) ---------------------


@contextlib.contextmanager
def _capture_audit_lines() -> Iterator[list[dict]]:
    """Collect the audit records the ``archivist`` logger emits, as parsed JSON.

    A handler on the ``archivist`` logger survives ``create_app`` (which only
    resets the root handlers), and each captured record is rendered through the
    real ``JsonFormatter`` and ``json.loads``-ed — so this asserts the audit line
    is genuinely parseable JSON, not just a Python record. Only the per-tool
    audit records carry a ``tool`` attribute, so MCP protocol chatter is ignored.
    """
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collect()
    logger = logging.getLogger("archivist")
    logger.addHandler(handler)
    lines: list[dict] = []
    try:
        yield lines
    finally:
        logger.removeHandler(handler)
        fmt = JsonFormatter()
        lines.extend(
            json.loads(fmt.format(r)) for r in records if getattr(r, "tool", None) is not None
        )


async def test_tool_call_emits_one_parseable_audit_line(db_engine: object) -> None:
    app = create_app()
    token = await _new_key("enrico")
    with _capture_audit_lines() as lines:
        async with _client_session(app, token) as session:
            await _call(session, "memory_upsert", project="p", keyword="kw", content="c")

    assert len(lines) == 1
    line = lines[0]
    assert line["tool"] == "memory_upsert"
    assert line["project"] == "p"
    assert line["keyword"] == "kw"
    assert line["key_owner"] == "enrico"
    assert line["outcome"] == "ok"
    assert isinstance(line["duration_ms"], int | float)


async def test_audit_line_records_the_error_outcome(db_engine: object) -> None:
    app = create_app()
    token = await _new_key()
    with _capture_audit_lines() as lines:
        async with _client_session(app, token) as session:
            # Append to a keyword that does not exist → NOT_FOUND.
            await _call(session, "memory_append", project="p", keyword="ghost", fragment="f")

    assert len(lines) == 1
    assert lines[0]["outcome"] == "NOT_FOUND"
    assert lines[0]["tool"] == "memory_append"


async def test_search_audit_line_records_result_count(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The search audit line carries result_count (T3.4 zero-result-rate metric)."""
    # Hybrid search is ON by default; inject FakeEmbedder so this contract test
    # never loads a real embedding model.
    from app import mcp_server
    from tests.fakes import FakeEmbedder

    monkeypatch.setattr(mcp_server, "_get_embedder", lambda: FakeEmbedder())

    app = create_app()
    token = await _new_key()
    with _capture_audit_lines() as lines:
        async with _client_session(app, token) as session:
            # Seed one entry, then a matching search (>=1 hit) and a miss (0 hits).
            await _call(
                session, "memory_upsert", project="p", keyword="redis", content="redis cache"
            )
            await _call(session, "memory_search", project="p", query="redis")
            await _call(session, "memory_search", project="p", query="nonexistent-term-xyz")

    searches = [line for line in lines if line["tool"] == "memory_search"]
    assert len(searches) == 2
    assert searches[0]["result_count"] >= 1
    assert searches[1]["result_count"] == 0


async def test_search_audit_line_records_intent(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory_search tags its audit line with the caller's read/write intent —
    the T3.4 follow-up that de-confounds the read-path zero-result rate. An
    omitted intent is recorded as ``unspecified`` (excluded from the corrected
    rate), so a forgotten tag never re-pollutes the read bucket."""
    # Hybrid search is ON by default; inject FakeEmbedder so this contract test
    # never loads a real embedding model.
    from app import mcp_server
    from tests.fakes import FakeEmbedder

    monkeypatch.setattr(mcp_server, "_get_embedder", lambda: FakeEmbedder())

    app = create_app()
    token = await _new_key()
    with _capture_audit_lines() as lines:
        async with _client_session(app, token) as session:
            await _call(session, "memory_search", project="p", query="a", intent="read")
            await _call(session, "memory_search", project="p", query="b", intent="write")
            await _call(session, "memory_search", project="p", query="c")  # default

    searches = [line for line in lines if line["tool"] == "memory_search"]
    assert [s["intent"] for s in searches] == ["read", "write", "unspecified"]


async def test_bad_intent_coerces_to_unspecified_without_breaking_search(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``intent`` is observational only: an unrecognized value is coerced to
    ``unspecified`` and the search still returns its hits — never a ValueError.
    An observability field must not break the operation it observes."""
    # Hybrid search is ON by default; inject FakeEmbedder so this contract test
    # never loads a real embedding model.
    from app import mcp_server
    from tests.fakes import FakeEmbedder

    monkeypatch.setattr(mcp_server, "_get_embedder", lambda: FakeEmbedder())

    app = create_app()
    token = await _new_key()
    with _capture_audit_lines() as lines:
        async with _client_session(app, token) as session:
            await _call(
                session, "memory_upsert", project="p", keyword="redis", content="redis cache"
            )
            result = await _call(
                session, "memory_search", project="p", query="redis", intent="bogus"
            )

    assert result.get("results"), "search must still return hits despite a bad intent"
    searches = [line for line in lines if line["tool"] == "memory_search"]
    assert len(searches) == 1
    assert searches[0]["intent"] == "unspecified"
    assert searches[0]["outcome"] == "ok"


async def test_search_audit_records_lexical_mode_when_disabled(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """search_mode is always on the audit line; lexical is what
    ENABLE_HYBRID_SEARCH=false falls back to (hybrid is the GA'd default,
    gap-closure S1 2026-07-04)."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ENABLE_HYBRID_SEARCH", "false")
    try:
        app = create_app()
        token = await _new_key()
        with _capture_audit_lines() as lines:
            async with _client_session(app, token) as session:
                await _call(session, "memory_search", project="p", query="auth", intent="read")
    finally:
        get_settings.cache_clear()

    searches = [line for line in lines if line["tool"] == "memory_search"]
    assert len(searches) == 1
    assert searches[0]["search_mode"] == "lexical"
    assert "vector_rescued" not in searches[0]


async def test_search_audit_records_hybrid_mode_when_enabled(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ENABLE_HYBRID_SEARCH on, the audit line switches to hybrid and gains
    vector_rescued — a dependency-free FakeEmbedder is injected via _get_embedder
    so the test never loads a real model."""
    from app import mcp_server
    from app.config import get_settings
    from tests.fakes import FakeEmbedder

    get_settings.cache_clear()
    monkeypatch.setenv("ENABLE_HYBRID_SEARCH", "true")
    monkeypatch.setattr(mcp_server, "_get_embedder", lambda: FakeEmbedder())
    try:
        app = create_app()
        token = await _new_key()
        with _capture_audit_lines() as lines:
            async with _client_session(app, token) as session:
                await _call(session, "memory_search", project="p", query="auth", intent="read")
    finally:
        get_settings.cache_clear()

    searches = [line for line in lines if line["tool"] == "memory_search"]
    assert len(searches) == 1
    assert searches[0]["search_mode"] == "hybrid"
    assert isinstance(searches[0]["vector_rescued"], bool)


async def test_each_tool_call_emits_exactly_one_audit_line(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hybrid search is ON by default; inject FakeEmbedder so this contract test
    # never loads a real embedding model.
    from app import mcp_server
    from tests.fakes import FakeEmbedder

    monkeypatch.setattr(mcp_server, "_get_embedder", lambda: FakeEmbedder())

    app = create_app()
    token = await _new_key()
    with _capture_audit_lines() as lines:
        async with _client_session(app, token) as session:
            await _call(session, "memory_upsert", project="p", keyword="kw", content="c")
            await _call(session, "memory_append", project="p", keyword="kw", fragment="f")
            await _call(session, "memory_search", project="p", query="kw")
            await _call(session, "memory_list", project="p")

    assert len(lines) == 4
    assert [line["tool"] for line in lines] == [
        "memory_upsert",
        "memory_append",
        "memory_search",
        "memory_list",
    ]


# --- near-duplicate advisory on memory_upsert create ------------------------


class _SameVecEmbedder:
    """Content-addressed FakeEmbedder maps distinct keywords to distinct vectors,
    and an identical keyword can't be re-created (ALREADY_EXISTS), so this
    same-vector double makes any two distinct keywords perfect near-duplicates.
    """

    dim = 384
    model_id = "same-vec"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]


async def test_upsert_create_returns_near_duplicates(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import mcp_server

    monkeypatch.setattr(mcp_server, "_get_embedder", lambda: _SameVecEmbedder())
    app = create_app()
    token = await _new_key()
    async with _client_session(app, token) as session:
        await _call(session, "memory_upsert", project="p", keyword="dev-server", content="run uvicorn")
        second = await _call(
            session, "memory_upsert", project="p", keyword="local-web-server", content="same thing"
        )
    assert second["created"] is True  # advisory, never a reject
    dups = second["near_duplicates"]
    assert dups[0]["keyword"] == "dev-server"
    assert dups[0]["similarity"] >= 0.99
    assert "run uvicorn" in dups[0]["preview"]


async def test_upsert_create_degrades_without_embedder(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import mcp_server

    def _boom() -> object:
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(mcp_server, "_get_embedder", _boom)
    app = create_app()
    token = await _new_key()
    async with _client_session(app, token) as session:
        result = await _call(session, "memory_upsert", project="p", keyword="solo", content="x")
    assert result["created"] is True
    assert "near_duplicates" not in result  # degrade: omit, never fail the write


async def test_upsert_create_degrades_when_dedup_lookup_fails(
    db_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: create_entry has already committed by the time the advisory dedup
    lookup runs — a failure there must never turn a successful write into an
    error response."""
    from app import mcp_server

    async def _boom_lookup(*args: object, **kwargs: object) -> None:
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(mcp_server, "_get_embedder", lambda: _SameVecEmbedder())
    monkeypatch.setattr(mcp_server.dedup_svc, "find_near_duplicates", _boom_lookup)
    app = create_app()
    token = await _new_key()
    async with _client_session(app, token) as session:
        result = await _call(session, "memory_upsert", project="p", keyword="solo", content="x")
    assert result["created"] is True
    assert "near_duplicates" not in result


# --- memory_history ----------------------------------------------------------


async def test_memory_history_tool_returns_versions(db_engine: object) -> None:
    token = await _new_key()
    app = create_app()
    async with _client_session(app, token) as session:
        await _call(session, "memory_upsert", project="proj", keyword="kw", content="v1")
        await _call(
            session, "memory_upsert",
            project="proj", keyword="kw", content="v2", expected_version=1,
        )

        payload = await _call(session, "memory_history", project="proj", keyword="kw")

    assert [v["version"] for v in payload["versions"]] == [2, 1]


async def test_memory_history_unknown_keyword_is_not_found(db_engine: object) -> None:
    token = await _new_key()
    app = create_app()
    async with _client_session(app, token) as session:
        payload = await _call(session, "memory_history", project="proj", keyword="nope")

    assert payload["error"]["code"] == "NOT_FOUND"


async def test_memory_history_include_deleted_recovers_pruned_content(
    db_engine: object,
) -> None:
    """The whole point of include_deleted is that an agent, over MCP, can
    recover content a forgetting sweep pruned. Cover both sides of the flag:
    default omitted -> NOT_FOUND for the pruned keyword; include_deleted=True
    -> the original content comes back."""
    token = await _new_key()
    app = create_app()
    async with _client_session(app, token) as session:
        await _call(
            session, "memory_upsert", project="proj", keyword="pruned-kw", content="original body"
        )
        await _call(session, "memory_delete", project="proj", keyword="pruned-kw")

        default_payload = await _call(
            session, "memory_history", project="proj", keyword="pruned-kw"
        )
        recovered = await _call(
            session,
            "memory_history",
            project="proj",
            keyword="pruned-kw",
            include_deleted=True,
        )

    assert default_payload["error"]["code"] == "NOT_FOUND"
    assert recovered["deleted"] is True
    assert recovered["versions"][0]["content"] == "original body"
