"""LLM write-engine: turns a news item into each system's native idiom.

One model (temp 0) drives refine/observation/merge for BOTH the Archivist and
claude-mem adapters, so they stay comparable to each other. The provider is
pluggable — Gemini Flash-Lite or Anthropic Haiku — selected by which API key is
set (see ``make_llm_client``). Token cost is counted with tiktoken on
(prompt + completion), a provider-agnostic comparability metric, so the choice
of provider changes only real $ cost and summary quality, never what the
benchmark measures.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Protocol

from benchmark.tokens import count_tokens
from benchmark.types import News

MODEL = "claude-haiku-4-5-20251001"
GEMINI_MODEL = "gemini-2.5-flash-lite"

# gemini-2.5-flash-lite free tier (2026): 15 RPM / 1,000 RPD / 250,000 TPM,
# per project, resets midnight Pacific. We throttle to stay under RPM and cap
# total calls under RPD; TPM is non-binding at our ~1-2k-token prompt sizes.
GEMINI_FREE_RPM = 13          # safety margin under the 15 RPM cap
GEMINI_FREE_MAX_CALLS = 950   # safety margin under the 1,000 RPD cap

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
    async def complete(self, system: str, user: str, max_tokens: int = 1024) -> str: ...


class WriteEngine:
    def __init__(self, client: LLMClient) -> None:
        self._client = client

    async def _run(self, system: str, user: str, max_tokens: int) -> tuple[str, int]:
        out = await self._client.complete(system, user, max_tokens=max_tokens)
        return out, count_tokens(system) + count_tokens(user) + count_tokens(out)

    async def refine_fragment(self, news: News) -> tuple[str, int]:
        return await self._run(_REFINE_SYS, f"[{news.date}] {news.title}\n{news.body}", max_tokens=1024)

    async def make_observation(self, news: News) -> tuple[str, int]:
        return await self._run(_OBS_SYS, f"[{news.date}] {news.title}\n{news.body}", max_tokens=1024)

    async def merge(self, keyword: str, current: str, fragments: list[str]) -> tuple[str, int]:
        joined = "\n---\n".join(fragments)
        user = f"KEYWORD: {keyword}\nCURRENT VALUE:\n{current}\n\nNEW FRAGMENTS:\n{joined}"
        return await self._run(_MERGE_SYS, user, max_tokens=8192)


class AnthropicClient:
    """Real Haiku 4.5 client (temperature 0)."""

    def __init__(self, api_key: str | None = None) -> None:
        import anthropic  # imported lazily so tests never need the SDK/key
        self._c = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        msg = await self._c.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if b.type == "text")


class FreeTierBudgetExceeded(RuntimeError):
    """Raised when the Gemini free-tier budget is reached — either our own RPD
    guard, or the server's daily-quota 429. The run stops cleanly; results
    through the last written checkpoint survive."""


def _is_daily_quota_error(e: object) -> bool:
    """True for a 429 whose quota is the free-tier PER-DAY request cap (terminal
    — retrying is futile until midnight Pacific), vs a transient per-minute 429.

    The `PerDay` quotaId sits in the error's nested `details`, not in `.message`
    (which names the `free_tier_requests` metric). Crucially that metric name is
    SHARED by the per-minute and per-day free-tier limits — only the quotaId
    differs (`...PerDay...` vs `...PerMinute...`) — so matching the metric name
    would misclassify a transient per-minute 429 as terminal and abort the run.
    Match `PerDay` alone, against the full repr (`str(e)` includes `details`)."""
    if getattr(e, "code", None) != 429:
        return False
    blob = f"{getattr(e, 'message', '') or ''}\n{e}"
    return "PerDay" in blob


class _RateBudget:
    """Async gate enforcing a min-interval (RPM cap) and a total-call ceiling
    (RPD cap). ``sleep``/``clock`` are injected so the pacing is unit-testable
    without real waiting."""

    def __init__(self, rpm: int, max_calls: int, *, sleep=asyncio.sleep, clock=time.monotonic) -> None:
        self._min_interval = 60.0 / rpm
        self._max_calls = max_calls
        self._sleep = sleep
        self._clock = clock
        self._calls = 0
        self._next_ok = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            if self._calls >= self._max_calls:
                raise FreeTierBudgetExceeded(
                    f"Gemini free-tier call budget ({self._max_calls}) reached — stopping "
                    "to stay under the 1,000 RPD cap. Results through the last checkpoint "
                    "are in results/. Resume tomorrow (quota resets midnight Pacific) or "
                    "scope the dataset down."
                )
            wait = self._next_ok - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._next_ok = self._clock() + self._min_interval
            self._calls += 1

    @property
    def calls(self) -> int:
        return self._calls


class GeminiClient:
    """Real Gemini Flash-Lite client (temperature 0), throttled to the free tier.

    Flash-Lite has thinking off by default, so the full ``max_output_tokens``
    budget goes to the answer — no thinking config needed. The SDK reads the key
    from ``GEMINI_API_KEY``/``GOOGLE_API_KEY`` if not passed explicitly. RPM and
    the daily call ceiling are overridable via ``GEMINI_RPM`` / ``GEMINI_MAX_CALLS``.
    """

    def __init__(self, api_key: str | None = None, rpm: int | None = None, max_calls: int | None = None) -> None:
        from google import genai  # imported lazily so tests never need the SDK/key

        self._genai = genai
        self._client = genai.Client(api_key=api_key or os.environ.get("GEMINI_API_KEY"))
        rpm = rpm if rpm is not None else int(os.environ.get("GEMINI_RPM", GEMINI_FREE_RPM))
        max_calls = max_calls if max_calls is not None else int(os.environ.get("GEMINI_MAX_CALLS", GEMINI_FREE_MAX_CALLS))
        self._gate = _RateBudget(rpm, max_calls)

    @property
    def calls(self) -> int:
        return self._gate.calls

    async def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        from google.genai import errors, types

        await self._gate.acquire()  # RPM throttle + RPD budget guard
        cfg = types.GenerateContentConfig(
            system_instruction=system, temperature=0, max_output_tokens=max_tokens
        )
        for attempt in range(5):
            try:
                resp = await self._client.aio.models.generate_content(
                    model=GEMINI_MODEL, contents=user, config=cfg
                )
                return resp.text or ""
            except errors.APIError as e:
                # Daily free-tier quota: terminal — fail fast so simulate() saves
                # partial results instead of the run burning ~75s of dead retries
                # then crashing with a raw ClientError (results lost).
                if _is_daily_quota_error(e):
                    raise FreeTierBudgetExceeded(
                        "Gemini free-tier DAILY request quota exhausted (429). This key "
                        "allows only ~20 requests/day for gemini-2.5-flash-lite — far below "
                        "the run's ~97-call need. Add prepaid credit (the whole run is ~$0.02 "
                        "of flash-lite) or use a higher-quota key. Detail: "
                        f"{str(getattr(e, 'message', '') or e)[:160]}"
                    ) from e
                if getattr(e, "code", None) in (429, 500, 503) and attempt < 4:
                    await asyncio.sleep(min(2 ** attempt * 5, 60))  # transient: back off
                    continue
                raise
        return ""


def make_llm_client() -> LLMClient:
    """Pick the write-engine LLM by which key is set. Gemini takes precedence
    (the benchmark's chosen low-cost engine); Anthropic is the fallback."""
    if os.environ.get("GEMINI_API_KEY"):
        return GeminiClient()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicClient()
    raise SystemExit(
        "No LLM key set — export GEMINI_API_KEY (preferred) or ANTHROPIC_API_KEY"
    )
