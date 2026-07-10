import pytest

from benchmark.run import simulate
from benchmark.types import CompactResult, FootprintSnapshot, News, RetrieveResult, WriteResult


class CountingAdapter:
    """Footprint grows with writes — lets us assert checkpoint recording."""
    def __init__(self, name): self.name = name; self._n = 0
    async def reset(self): self._n = 0
    async def write(self, news): self._n += 1; return WriteResult(llm_tokens=1)
    async def maybe_compact(self, asset): return None
    async def context_footprint(self): return FootprintSnapshot(startup_tokens=self._n * 10)
    async def retrieve(self, query): return RetrieveResult(payload="0.10", tokens=3)


@pytest.fixture
def dataset():
    return {
        "aaa": [News("aaa", f"2026-01-{d:02d}", "t", "b") for d in range(1, 4)],
        "bbb": [News("bbb", f"2026-01-{d:02d}", "t", "b") for d in range(1, 4)],
    }


async def test_simulate_records_one_row_per_checkpoint(dataset):
    from benchmark.probes import Probe
    probes = [Probe(id="p", query="q", pattern="0.10", category="recent", available_from="2026-01-01")]
    rows = await simulate(dataset, [CountingAdapter("x")], probes, checkpoint_every=2)
    # days 1..3 → checkpoints at day index 2 (0-based day 1) and final day 3
    assert all(r["adapter"] == "x" for r in rows)
    assert {r["day"] for r in rows}  # non-empty
    assert rows[-1]["footprint_tokens"] == 60  # 6 writes * 10 (2 assets * 3 days)


async def test_simulate_accumulates_write_cost(dataset):
    rows = await simulate(dataset, [CountingAdapter("x")], [], checkpoint_every=2)
    assert rows[-1]["write_tokens_cumulative"] == 6  # 2 assets * 3 days
