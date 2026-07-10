import pytest

from benchmark.probes import Probe, evaluate
from benchmark.types import RetrieveResult


class StubAdapter:
    name = "stub"
    def __init__(self, payload): self._payload = payload
    async def retrieve(self, query): return RetrieveResult(payload=self._payload, tokens=10)


@pytest.fixture
def probes():
    return [
        Probe(id="p1", query="dividend aaa", pattern=r"0\.10", category="recent", available_from="2026-01-01"),
        Probe(id="p2", query="old aaa", pattern=r"0\.05", category="old", available_from="2026-01-01"),
    ]


async def test_recall_counts_pattern_hits(probes):
    rep = await evaluate(StubAdapter("dividend was 0.10 per share"), probes, as_of_date="2026-02-01")
    assert rep.hits == 1 and rep.total == 2
    assert rep.recall == 0.5


async def test_survival_recall_uses_only_old_probes(probes):
    rep = await evaluate(StubAdapter("value 0.05 retained"), probes, as_of_date="2026-02-01")
    assert rep.survival_recall == 1.0  # the single 'old' probe hit


async def test_probe_excluded_before_available_from(probes):
    rep = await evaluate(StubAdapter("0.10 and 0.05"), probes, as_of_date="2025-12-01")
    assert rep.total == 0  # nothing available yet
