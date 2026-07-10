import pytest

from benchmark.adapters.rag_wiki import RagWikiAdapter
from benchmark.types import News
from tests.test_adapter_contract import assert_adapter_contract


@pytest.fixture
def news():
    return News(asset="aaa", date="2026-01-01", title="AAA dividend 0.10", body="Dividend of $0.10 declared.")


async def test_wiki_satisfies_contract(tmp_path, news):
    adapter = RagWikiAdapter(db_path=str(tmp_path / "wiki.db"))
    await assert_adapter_contract(adapter, news)


async def test_wiki_startup_footprint_is_zero(tmp_path, news):
    adapter = RagWikiAdapter(db_path=str(tmp_path / "wiki.db"))
    await adapter.reset()
    await adapter.write(news)
    fp = await adapter.context_footprint()
    assert fp.startup_tokens == 0  # retrieval-only: cost is paid per query


async def test_wiki_retrieve_finds_written_fact(tmp_path, news):
    adapter = RagWikiAdapter(db_path=str(tmp_path / "wiki.db"))
    await adapter.reset()
    await adapter.write(news)
    res = await adapter.retrieve("dividend")
    assert "0.10" in res.payload
    assert res.tokens > 0
