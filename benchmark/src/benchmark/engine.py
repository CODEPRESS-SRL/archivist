"""LLM write-engine: turns a news item into each system's native idiom.

One model (Haiku 4.5, temp 0) drives refine/observation/merge. Token cost is
counted with tiktoken on (prompt + completion) so it is comparable across
systems regardless of provider usage reporting.
"""
from __future__ import annotations

from typing import Protocol

from benchmark.tokens import count_tokens
from benchmark.types import News

MODEL = "claude-haiku-4-5-20251001"

_REFINE_SYS = (
    "You maintain a shared keyword memory. Compress the news into a dense, "
    "self-contained fragment under 500 tokens. Keep every concrete fact "
    "(dates, numbers, ratings). No preamble."
)
_OBS_SYS = (
    "You record a memory observation. Write a concise title line then the key "
    "facts of the news as an observation. Keep concrete numbers and dates."
)
_MERGE_SYS = (
    "You compact a keyword's memory. Merge the current value and the new "
    "fragments into one dense value under 8000 tokens. Preserve every distinct "
    "fact; drop only redundancy. No preamble."
)


class LLMClient(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


class WriteEngine:
    def __init__(self, client: LLMClient) -> None:
        self._client = client

    async def _run(self, system: str, user: str) -> tuple[str, int]:
        out = await self._client.complete(system, user)
        return out, count_tokens(system) + count_tokens(user) + count_tokens(out)

    async def refine_fragment(self, news: News) -> tuple[str, int]:
        return await self._run(_REFINE_SYS, f"[{news.date}] {news.title}\n{news.body}")

    async def make_observation(self, news: News) -> tuple[str, int]:
        return await self._run(_OBS_SYS, f"[{news.date}] {news.title}\n{news.body}")

    async def merge(self, keyword: str, current: str, fragments: list[str]) -> tuple[str, int]:
        joined = "\n---\n".join(fragments)
        user = f"KEYWORD: {keyword}\nCURRENT VALUE:\n{current}\n\nNEW FRAGMENTS:\n{joined}"
        return await self._run(_MERGE_SYS, user)


class AnthropicClient:
    """Real Haiku 4.5 client (temperature 0)."""

    def __init__(self, api_key: str | None = None) -> None:
        import anthropic  # imported lazily so tests never need the SDK/key
        self._c = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(self, system: str, user: str) -> str:
        msg = await self._c.messages.create(
            model=MODEL,
            max_tokens=1500,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if b.type == "text")
