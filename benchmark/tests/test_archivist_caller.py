"""Unit tests for the raw stateless-HTTP tool-caller and its result parsing."""
import json
from dataclasses import dataclass, field

import httpx
import pytest

from benchmark.adapters.archivist import _extract_rpc_result, _parse_tool_result, make_mcp_caller


@dataclass
class _TextBlock:
    text: str


@dataclass
class _Result:
    structuredContent: dict | None = None
    content: list = field(default_factory=list)


def test_prefers_structured_content_when_set():
    r = _Result(structuredContent={"entries": [{"keyword": "nvda"}]})
    assert _parse_tool_result(r) == {"entries": [{"keyword": "nvda"}]}


def test_parses_json_from_text_content_when_structured_is_empty():
    # The prod server's real shape: JSON string in a text block, no structuredContent.
    payload = '{"entries": [{"keyword": "nvda", "token_count": 67}]}'
    r = _Result(structuredContent=None, content=[_TextBlock(payload)])
    out = _parse_tool_result(r)
    assert out["entries"][0]["keyword"] == "nvda"
    assert out["entries"][0]["token_count"] == 67


def test_empty_result_returns_empty_dict():
    assert _parse_tool_result(_Result()) == {}


def test_non_json_text_returns_empty_dict():
    r = _Result(content=[_TextBlock("not json at all")])
    assert _parse_tool_result(r) == {}


def test_json_list_is_wrapped():
    r = _Result(content=[_TextBlock("[1, 2, 3]")])
    assert _parse_tool_result(r) == {"result": [1, 2, 3]}


# --- JSON-RPC / SSE envelope extraction ----------------------------------------

_REQ = httpx.Request("POST", "http://u/mcp")


def _resp(status, **kw):
    return httpx.Response(status, request=_REQ, **kw)


def _sse(result_obj):
    env = {"jsonrpc": "2.0", "id": 1, "result": result_obj}
    return _resp(200, headers={"content-type": "text/event-stream"},
                 text=f"event: message\r\ndata: {json.dumps(env)}\r\n\r\n")


def test_extract_parses_sse_text_content():
    inner = {"entries": [{"keyword": "nvda", "token_count": 12}]}
    resp = _sse({"content": [{"type": "text", "text": json.dumps(inner)}]})
    assert _extract_rpc_result(resp) == inner


def test_extract_parses_plain_json_response():
    inner = {"deleted": True}
    env = {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": inner}}
    resp = _resp(200, headers={"content-type": "application/json"}, json=env)
    assert _extract_rpc_result(resp) == inner


def test_extract_raises_on_jsonrpc_error():
    env = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}
    resp = httpx.Response(200, headers={"content-type": "text/event-stream"},
                          text=f"data: {json.dumps(env)}\n")
    with pytest.raises(RuntimeError, match="MCP error"):
        _extract_rpc_result(resp)


# --- raw caller retry/backoff via an injected fake httpx client -----------------

class _FakeClient:
    """Async httpx-like client replaying a scripted sequence of Responses/errors."""
    def __init__(self, script):
        self._script = list(script)
        self.posts = 0

    async def post(self, url, headers=None, json=None):
        self.posts += 1
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    async def aclose(self):
        pass


async def _nosleep(_): pass


async def test_caller_returns_parsed_tool_payload():
    inner = {"entries": [{"keyword": "aapl"}]}
    client = _FakeClient([_sse({"content": [{"type": "text", "text": json.dumps(inner)}]})])
    call, aclose = make_mcp_caller("u", "k", max_rpm=0, _client=client, _sleep=_nosleep)
    assert await call("memory_list", {"project": "p"}) == inner
    assert client.posts == 1
    await aclose()


async def test_caller_retries_on_429_then_succeeds():
    ok = _sse({"structuredContent": {"ok": 1}})
    client = _FakeClient([_resp(429), ok])
    call, _ = make_mcp_caller("u", "k", max_rpm=0, _client=client, _sleep=_nosleep)
    assert await call("t", {}) == {"ok": 1}
    assert client.posts == 2


async def test_caller_retries_on_transport_error_then_succeeds():
    ok = _sse({"structuredContent": {"ok": 2}})
    client = _FakeClient([httpx.ConnectError("refused"), ok])
    call, _ = make_mcp_caller("u", "k", max_rpm=0, _client=client, _sleep=_nosleep)
    assert await call("t", {}) == {"ok": 2}
    assert client.posts == 2


async def test_caller_gives_up_after_persistent_5xx():
    client = _FakeClient([_resp(503)] * 5)
    call, _ = make_mcp_caller("u", "k", max_rpm=0, _client=client, _sleep=_nosleep)
    with pytest.raises(httpx.HTTPStatusError):
        await call("t", {})
    assert client.posts == 5
