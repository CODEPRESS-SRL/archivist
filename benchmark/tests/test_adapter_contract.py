from benchmark.types import News, WriteResult, FootprintSnapshot, RetrieveResult


async def assert_adapter_contract(adapter, news: News) -> None:
    """Every adapter must satisfy this regardless of backend."""
    await adapter.reset()
    fp0 = await adapter.context_footprint()
    assert isinstance(fp0, FootprintSnapshot)
    wr = await adapter.write(news)
    assert isinstance(wr, WriteResult)
    res = await adapter.retrieve(news.title)
    assert isinstance(res, RetrieveResult)
    assert res.tokens >= 0
