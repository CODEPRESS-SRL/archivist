#!/usr/bin/env python
"""Export the frozen MCP contract consumed by the Node service.

Writes ``tests/contract/mcp-contract.json``: the server instructions, the seven
tools (name, description, input schema, in registration order) as FastMCP
publishes them, and slug vectors produced by ``normalize_keyword``.

    python scripts/export_mcp_contract.py            # rewrite the file
    python scripts/export_mcp_contract.py --stdout   # print instead

``tests/test_mcp_contract.py`` fails if the committed file drifts from the code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_API_DIR = Path(__file__).resolve().parent.parent
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))

from app.mcp_server import _INSTRUCTIONS, build_mcp  # noqa: E402
from app.utils.keywords import normalize_keyword  # noqa: E402

CONTRACT_FILE = _API_DIR / "tests" / "contract" / "mcp-contract.json"

SLUG_INPUTS: list[str] = [
    "Value School",
    "value_school",
    "  X  ",
    "à-é",
    "emoji🙂ok",
    "---",
    "",
    "a" * 80,
    "A--B__C",
    "billing-api",
    "UPPER_snake Mix",
    "snake_case_key",
    "café",
    "naïve café",
    "hello 🌍 world",
    "a@b#c",
    "--a--b--",
    "v1.2.3",
    "   ",
    "x" * 64,
    "x" * 65,
    ("a" * 63) + "-b",
    ("a" * 63) + " b",
    "tab\tsep",
    "new\nline",
    "ÀÉÎ",
    "ß",
    "İstanbul",
    "日本語",
    "a-_-b",
]


async def _tools() -> list[dict[str, Any]]:
    tools = await build_mcp().list_tools()
    return [
        {"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools
    ]


def _slug(raw: str) -> str | None:
    try:
        return normalize_keyword(raw)
    except ValueError:
        return None


def build_contract() -> dict[str, Any]:
    return {
        "istruzioni": _INSTRUCTIONS,
        "strumenti": asyncio.run(_tools()),
        "vettoriSlug": [[raw, _slug(raw)] for raw in SLUG_INPUTS],
    }


def render(contract: dict[str, Any]) -> str:
    """Deterministic serialization: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(contract, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdout", action="store_true", help="print instead of writing the file")
    args = parser.parse_args()
    text = render(build_contract())
    if args.stdout:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stdout.write(text)
        return
    CONTRACT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT_FILE.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {CONTRACT_FILE}")


if __name__ == "__main__":
    main()
