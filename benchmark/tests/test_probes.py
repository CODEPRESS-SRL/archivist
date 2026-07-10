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


def test_make_probes_cli_writes_reviewable_file(tmp_path, monkeypatch):
    import json
    import sys

    from benchmark import probes as P

    ds = tmp_path / "ds.jsonl"
    ds.write_text(
        json.dumps({
            "asset": "aaa", "date": "2026-01-01", "title": "AAA dividend",
            "body": "Dividend of $0.10.",
            "facts": [{"type": "dividend", "value": "0.10", "unit": "USD"}],
        }) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "probes.json"
    monkeypatch.setattr(sys, "argv", ["make_probes", "--dataset", str(ds), "--out", str(out)])
    P.main()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["query"] == "dividend aaa"
    assert "0" in data[0]["pattern"]  # escaped value present
