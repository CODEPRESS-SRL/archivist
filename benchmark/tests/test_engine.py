import pytest

from benchmark.engine import FreeTierBudgetExceeded, WriteEngine, _RateBudget
from benchmark.types import News


class FakeTime:
    """Deterministic clock+sleep for testing the rate limiter without real waits."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        self.calls.append((system, user))
        return "REFINED: " + user[:20]


@pytest.fixture
def news():
    return News(asset="aaa", date="2026-01-01", title="AAA dividend", body="Dividend of $0.10.")


async def test_refine_returns_text_and_positive_token_count(news):
    eng = WriteEngine(FakeLLM())
    text, tokens = await eng.refine_fragment(news)
    assert text.startswith("REFINED:")
    assert tokens > 0


async def test_merge_passes_current_and_fragments_to_llm(news):
    llm = FakeLLM()
    eng = WriteEngine(llm)
    text, tokens = await eng.merge("aaa", "old value", ["frag1", "frag2"])
    assert "old value" in llm.calls[0][1]
    assert "frag1" in llm.calls[0][1]
    assert tokens > 0


def test_make_llm_client_prefers_gemini(monkeypatch):
    import benchmark.engine as eng
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr(eng, "GeminiClient", lambda: "gemini")
    monkeypatch.setattr(eng, "AnthropicClient", lambda: "anthropic")
    assert eng.make_llm_client() == "gemini"


def test_make_llm_client_falls_back_to_anthropic(monkeypatch):
    import benchmark.engine as eng
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setattr(eng, "GeminiClient", lambda: "gemini")
    monkeypatch.setattr(eng, "AnthropicClient", lambda: "anthropic")
    assert eng.make_llm_client() == "anthropic"


def test_make_llm_client_errors_without_key(monkeypatch):
    import benchmark.engine as eng
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        eng.make_llm_client()


async def test_rate_budget_paces_to_rpm():
    ft = FakeTime()
    gate = _RateBudget(rpm=12, max_calls=100, sleep=ft.sleep, clock=ft.clock)  # 5s spacing
    for _ in range(3):
        await gate.acquire()
    assert ft.sleeps == [5.0, 5.0]  # first call free, next two paced


async def test_rate_budget_stops_at_max_calls_rpd_guard():
    ft = FakeTime()
    gate = _RateBudget(rpm=600, max_calls=3, sleep=ft.sleep, clock=ft.clock)
    for _ in range(3):
        await gate.acquire()
    assert gate.calls == 3
    with pytest.raises(FreeTierBudgetExceeded):
        await gate.acquire()


class _FakeApiError(Exception):
    def __init__(self, code, message, details=""):
        self.code = code
        self.message = message
        self._details = details
        super().__init__(message)

    def __str__(self):
        # Mimic google.genai APIError.__str__, which appends the raw details JSON
        # (where the PerDay/PerMinute quotaId actually lives) after the message.
        return f"{self.code}. {self.message} {self._details}".strip()


def test_is_daily_quota_error_only_for_perday_429():
    from benchmark.engine import _is_daily_quota_error
    daily = _FakeApiError(429, "Quota exceeded ... GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    per_min = _FakeApiError(429, "Resource exhausted: per-minute rate limit")
    server = _FakeApiError(503, "Service unavailable")
    assert _is_daily_quota_error(daily) is True
    assert _is_daily_quota_error(per_min) is False
    assert _is_daily_quota_error(server) is False


def test_is_daily_quota_error_perday_in_details_but_not_perminute():
    """Regression for the real google-genai 429 shape: the daily and per-minute
    free-tier limits BOTH name `free_tier_requests` in `.message`; only the
    `details.quotaId` distinguishes them (`...PerDay...` vs `...PerMinute...`).
    The daily one (terminal) must be caught; the per-minute one (transient, the
    retry/backoff branch's job) must NOT be — else a RPM blip aborts the run."""
    from benchmark.engine import _is_daily_quota_error
    msg = (
        "You exceeded your current quota. Quota exceeded for metric: "
        "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20"
    )
    daily = _FakeApiError(429, msg, details="quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    per_min = _FakeApiError(429, msg, details="quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
    assert _is_daily_quota_error(daily) is True
    assert _is_daily_quota_error(per_min) is False
