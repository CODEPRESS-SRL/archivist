import pytest

from benchmark.engine import WriteEngine
from benchmark.types import News


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
