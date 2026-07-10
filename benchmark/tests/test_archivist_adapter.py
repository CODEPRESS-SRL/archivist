import pytest

from benchmark.adapters.archivist import ArchivistAdapter
from benchmark.types import News


class FakeArchivist:
    """In-memory stand-in mirroring the REAL Archivist MCP response shapes."""

    def __init__(self):
        self.entries: dict[str, dict] = {}  # kw -> {content, version, fragments:[{id,content}]}
        self._next_frag_id = 1

    async def call_tool(self, name: str, args: dict) -> dict:
        kw = args.get("keyword")
        if name == "memory_get":
            keywords = args["keywords"]
            keywords = [keywords] if isinstance(keywords, str) else list(keywords)
            out, not_found = [], []
            for k in keywords:
                e = self.entries.get(k)
                if e is None:
                    not_found.append(k)
                    continue
                frags = e["fragments"]
                out.append({
                    "keyword": k, "content": e["content"], "version": e["version"],
                    "fragments": frags, "fragment_count": len(frags),
                    "last_fragment_id": frags[-1]["id"] if frags else None,
                    "compaction_due": len(frags) > 3, "token_total": 0, "age_days": 0,
                })
            return {"entries": out, "not_found": not_found}
        if name == "memory_upsert":
            content = args["content"]
            if args.get("expected_version") is None:
                self.entries[kw] = {"content": content, "version": 1, "fragments": []}
            else:
                e = self.entries[kw]
                e["content"], e["fragments"] = content, []
                e["version"] += 1
            return {"keyword": kw, "version": self.entries[kw]["version"], "compaction_due": False}
        if name == "memory_append":
            e = self.entries[kw]
            e["fragments"].append({"id": self._next_frag_id, "content": args["fragment"]})
            self._next_frag_id += 1
            return {"keyword": kw, "compaction_due": len(e["fragments"]) > 3}
        if name == "memory_list":
            return {"project": args["project"], "entries": [
                {"keyword": k, "token_count": 10, "pending_fragments": len(e["fragments"]),
                 "compaction_due": len(e["fragments"]) > 3, "updated_at": None}
                for k, e in self.entries.items()
            ]}
        if name == "memory_search":
            q = args["query"].lower()
            results = [
                {"keyword": k, "score": 1.0, "excerpt": e["content"][:50],
                 "pending_fragments": len(e["fragments"]), "token_count": 10, "age_days": 0}
                for k, e in self.entries.items()
                if q in k.lower() or q in e["content"].lower()
            ]
            return {"results": results, "related_keywords": []}
        if name == "memory_delete":
            self.entries.pop(kw, None)
            return {"deleted": True}
        return {}


class FakeEngine:
    async def refine_fragment(self, news):
        return (f"frag:{news.title}", 5)

    async def merge(self, keyword, current, fragments):
        return ("merged", 7)


@pytest.fixture
def news():
    return News(asset="aaa", date="2026-01-01", title="AAA dividend", body="Dividend of $0.10.")


async def test_first_write_upserts_then_appends(news):
    fake = FakeArchivist()
    a = ArchivistAdapter(call_tool=fake.call_tool, engine=FakeEngine(), project="bench")
    await a.write(news)
    assert "aaa" in fake.entries and fake.entries["aaa"]["version"] == 1
    await a.write(news)
    assert len(fake.entries["aaa"]["fragments"]) == 1


async def test_footprint_counts_list_plus_hot_keys(news):
    fake = FakeArchivist()
    a = ArchivistAdapter(call_tool=fake.call_tool, engine=FakeEngine(), project="bench")
    await a.write(news)
    fp = await a.context_footprint()
    assert fp.startup_tokens > 0


async def test_maybe_compact_merges_when_due(news):
    fake = FakeArchivist()
    a = ArchivistAdapter(call_tool=fake.call_tool, engine=FakeEngine(), project="bench")
    await a.write(news)          # upsert v1
    for _ in range(4):           # 4 appends -> compaction_due (>3)
        await a.write(news)
    assert a._compaction_due["aaa"] is True
    res = await a.maybe_compact("aaa")
    assert res is not None and res.merged
    assert fake.entries["aaa"]["version"] == 2      # bumped
    assert fake.entries["aaa"]["fragments"] == []   # consumed
    assert a._compaction_due["aaa"] is False
    assert await a.maybe_compact("aaa") is None      # no-op second time


async def test_retrieve_does_search_then_get(news):
    fake = FakeArchivist()
    a = ArchivistAdapter(call_tool=fake.call_tool, engine=FakeEngine(), project="bench")
    await a.write(news)
    res = await a.retrieve("aaa")
    assert "AAA dividend" in res.payload
    assert res.tokens > 0


async def test_reset_deletes_all(news):
    fake = FakeArchivist()
    a = ArchivistAdapter(call_tool=fake.call_tool, engine=FakeEngine(), project="bench")
    await a.write(news)
    await a.reset()
    assert fake.entries == {}
