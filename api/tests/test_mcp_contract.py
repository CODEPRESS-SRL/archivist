"""The frozen MCP contract (tool surface + slug vectors) must match the live code.

The Node service republishes the seven memory tools from ``mcp-contract.json``
and normalizes slugs with a TypeScript copy of ``normalize_keyword``. This test
fails whenever the file drifts from ``build_mcp()`` / ``_INSTRUCTIONS`` /
``normalize_keyword``. Regenerate with ``python scripts/export_mcp_contract.py``.

No database: nothing here uses the ``db_engine`` family of fixtures (they are
opt-in, not autouse), so Docker is not needed.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_API_DIR = Path(__file__).resolve().parent.parent
_CONTRACT_FILE = _API_DIR / "tests" / "contract" / "mcp-contract.json"
_SCRIPT = _API_DIR / "scripts" / "export_mcp_contract.py"

_TOOLS = [
    "memory_search",
    "memory_get",
    "memory_append",
    "memory_upsert",
    "memory_list",
    "memory_delete",
    "memory_history",
]

_REQUIRED_VECTORS = [
    "Value School",
    "value_school",
    "  X  ",
    "à-é",
    "emoji🙂ok",
    "---",
    "",
    "a" * 80,
    "A--B__C",
]


def _load_exporter():
    spec = importlib.util.spec_from_file_location("export_mcp_contract", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contract_file_matches_live_code() -> None:
    committed = json.loads(_CONTRACT_FILE.read_text(encoding="utf-8"))
    live = json.loads(json.dumps(_load_exporter().build_contract()))
    assert committed == live


def test_contract_file_is_the_canonical_serialization() -> None:
    exporter = _load_exporter()
    assert _CONTRACT_FILE.read_text(encoding="utf-8") == exporter.render(exporter.build_contract())


def test_contract_has_the_seven_tools_in_registration_order() -> None:
    committed = json.loads(_CONTRACT_FILE.read_text(encoding="utf-8"))
    assert [t["name"] for t in committed["strumenti"]] == _TOOLS
    for tool in committed["strumenti"]:
        assert set(tool) == {"name", "description", "inputSchema"}
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_contract_vectors_cover_required_cases_and_match_normalize_keyword() -> None:
    from app.utils.keywords import normalize_keyword

    committed = json.loads(_CONTRACT_FILE.read_text(encoding="utf-8"))
    vectors = committed["vettoriSlug"]
    inputs = [pair[0] for pair in vectors]
    for required in _REQUIRED_VECTORS:
        assert required in inputs
    for raw, expected in vectors:
        try:
            assert normalize_keyword(raw) == expected
        except ValueError:
            assert expected is None
    # Both outcomes are exercised: a normal slug and a rejected one.
    assert any(pair[1] is None for pair in vectors)
    assert any(pair[1] is not None for pair in vectors)
