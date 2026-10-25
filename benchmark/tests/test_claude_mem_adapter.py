import pytest

from benchmark.adapters.claude_mem import ClaudeMemAdapter
from benchmark.types import News


class FakeClaudeMem:
    def __init__(self):
        self.obs: list[dict] = []

    async def call_tool(self, name: str, args: dict) -> dict:
        if name == "save_memory":
            self.obs.append({"text": args["content"], "tag": args.get("tag")})
            return {"saved": True}
        if name == "search":
            hits = [o for o in self.obs if args["query"].lower() in o["text"].lower()]
            return {"results": [{"text": o["text"]} for o in hits]}
        if name == "context_index":  # footprint: the SessionStart injected index
            return {"index": "\n".join(o["text"][:40] for o in self.obs)}
        return {}


class FakeEngine:
    async def make_observation(self, news): return (f"obs:{news.title} {news.body}", 6)


@pytest.fixture
def news():
    return News(asset="aaa", date="2026-01-01", title="AAA dividend", body="Dividend of $0.10.")


async def test_write_saves_observation_and_accumulates(news):
    fake = FakeClaudeMem()
    a = ClaudeMemAdapter(call_tool=fake.call_tool, engine=FakeEngine(), tag="bench")
    await a.write(news)
    await a.write(news)
    assert len(fake.obs) == 2  # no compaction: it accumulates


async def test_maybe_compact_is_noop(news):
    a = ClaudeMemAdapter(call_tool=FakeClaudeMem().call_tool, engine=FakeEngine(), tag="bench")
    assert await a.maybe_compact("aaa") is None


async def test_footprint_grows_with_observations(news):
    fake = FakeClaudeMem()
    a = ClaudeMemAdapter(call_tool=fake.call_tool, engine=FakeEngine(), tag="bench")
    await a.write(news)
    fp1 = (await a.context_footprint()).startup_tokens
    await a.write(news)
    fp2 = (await a.context_footprint()).startup_tokens
    assert fp2 > fp1  # the growth this benchmark exists to quantify
