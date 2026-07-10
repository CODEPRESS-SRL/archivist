"""Unit tests for the MCP result normalization used by the archivist adapter."""
from dataclasses import dataclass, field

from benchmark.adapters.archivist import _parse_tool_result


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
