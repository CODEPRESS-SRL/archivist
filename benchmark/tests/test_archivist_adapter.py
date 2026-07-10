import pytest

from benchmark.adapters.archivist import ArchivistAdapter
from benchmark.types import News


class FakeArchivist:
    """Minimal in-memory stand-in for the Archivist MCP tools."""
    def __init__(self):
        self.store: dict[str, dict] = {}
        self.frags: dict[str, list[str]] = {}

    async def call_tool(self, name: str, args: dict) -> dict:
        kw = args.get("keyword") or (args.get("keywords") if isinstance(args.get("keywords"), str) else None)
        if name == "memory_get":
            e = self.store.get(kw)
            return {"entries": [e] if e else []}
        if name == "memory_upsert":
            self.store[kw] = {"keyword": kw, "value": args["value"], "version": 1}
            self.frags[kw] = []
            return {"keyword": kw, "version": 1, "compaction_due": False}
        if name == "memory_append":
            self.frags.setdefault(kw, []).append(args["fragment"])
            due = len(self.frags[kw]) > 3
            return {"keyword": kw, "compaction_due": due}
        if name == "memory_list":
            return {"entries": [{"keyword": k, "size_tokens": 10} for k in self.store]}
        return {}


class FakeEngine:
    async def refine_fragment(self, news): return (f"frag:{news.title}", 5)
    async def merge(self, keyword, current, fragments): return ("merged", 7)


@pytest.fixture
def news():
    return News(asset="aaa", date="2026-01-01", title="AAA dividend", body="Dividend of $0.10.")


async def test_first_write_upserts_then_appends(news):
    fake = FakeArchivist()
    a = ArchivistAdapter(call_tool=fake.call_tool, engine=FakeEngine(), project="bench")
    await a.write(news)
    assert "aaa" in fake.store  # keyword == asset slug


async def test_footprint_counts_list_plus_hot_keys(news):
    fake = FakeArchivist()
    a = ArchivistAdapter(call_tool=fake.call_tool, engine=FakeEngine(), project="bench")
    await a.write(news)
    fp = await a.context_footprint()
    assert fp.startup_tokens > 0
