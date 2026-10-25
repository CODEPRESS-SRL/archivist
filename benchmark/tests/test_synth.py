"""Tests for the synthetic long-horizon dataset builder (synth.py).

The builder resamples REAL articles over a synthetic timeline: every emitted
item IS a real article (real body + real facts), only the date and the arrival
pattern (novelty / revision / restatement) are synthetic. These tests pin the
arrival model's contract — determinism, novelty monotonicity, saturation at
pool exhaustion, and shape compatibility with the simulate() load path.
"""
from benchmark.synth import build_run_inputs, build_synthetic_dataset
from benchmark.types import Fact, News


def _real():
    """A tiny hand-made 'real' corpus: aaa has 4 pct articles + 1 usd, bbb has 2."""
    def n(asset, i, facts):
        return News(asset=asset, date=f"2026-01-{i:02d}", title=f"{asset}-{i}",
                    body=f"{asset} article {i}", facts=facts)
    return {
        "aaa": [
            n("aaa", 1, (Fact("pct", "1.0%", "%"),)),
            n("aaa", 2, (Fact("pct", "2.0%", "%"),)),
            n("aaa", 3, (Fact("pct", "3.0%", "%"),)),
            n("aaa", 4, (Fact("pct", "4.0%", "%"),)),
            n("aaa", 5, (Fact("usd", "$5 billion"),)),
        ],
        "bbb": [
            n("bbb", 1, (Fact("pct", "1.5%", "%"),)),
            n("bbb", 2, (Fact("rating", "upgrade"),)),
        ],
    }


def _distinct_bodies(items):
    return {i.body for i in items}


def test_deterministic_same_seed_identical():
    kw = dict(writes_per_keyword=30, novelty_rate=0.5, revision_rate=0.1, seed=7)
    a = build_synthetic_dataset(_real(), **kw)
    b = build_synthetic_dataset(_real(), **kw)
    assert a == b


def test_shape_loads_like_simulate_expects():
    ds = build_synthetic_dataset(_real(), writes_per_keyword=25, novelty_rate=0.5, seed=1)
    assert set(ds) == {"aaa", "bbb"}
    for asset, items in ds.items():
        assert len(items) == 25
        assert all(isinstance(i, News) and i.asset == asset for i in items)
        # simulate() is last-wins per (asset, date): dates MUST be distinct.
        assert len({i.date for i in items}) == len(items)
        # every emitted item carries a real article's facts verbatim.
        assert all(i.facts for i in items)


def test_restatement_only_when_no_novelty_or_revision():
    # lambda=0, rho=0 -> after the forced first article, everything is a restatement
    # of it: exactly one distinct article, repeated (drives fragment accumulation).
    ds = build_synthetic_dataset(_real(), writes_per_keyword=20,
                                 novelty_rate=0.0, revision_rate=0.0, seed=3)
    assert len(_distinct_bodies(ds["aaa"])) == 1
    assert len(ds["aaa"]) == 20


def test_novelty_monotonic_in_lambda():
    lo = build_synthetic_dataset(_real(), writes_per_keyword=20,
                                 novelty_rate=0.1, revision_rate=0.0, seed=5)
    hi = build_synthetic_dataset(_real(), writes_per_keyword=20,
                                 novelty_rate=0.9, revision_rate=0.0, seed=5)
    assert len(_distinct_bodies(hi["aaa"])) >= len(_distinct_bodies(lo["aaa"]))


def test_novelty_saturates_at_pool_size():
    # lambda=1.0 over N far larger than the pool: distinct articles cannot exceed
    # the real pool size (5 for aaa); the rest are repeats.
    ds = build_synthetic_dataset(_real(), writes_per_keyword=60,
                                 novelty_rate=1.0, revision_rate=0.0, seed=2)
    assert len(_distinct_bodies(ds["aaa"])) == 5
    assert len(ds["aaa"]) == 60
    assert len(_distinct_bodies(ds["bbb"])) == 2  # bbb pool is only 2


def test_revision_advances_within_shared_fact_type():
    # lambda=0, rho=1.0: revisions emit UNUSED real articles sharing a fact type
    # with an already-emitted one. Seed article is aaa-1 (pct); revisions reach the
    # other 3 pct articles but NOT the usd one -> 4 distinct, then restatements.
    ds = build_synthetic_dataset(_real(), writes_per_keyword=20,
                                 novelty_rate=0.0, revision_rate=1.0, seed=4)
    assert len(_distinct_bodies(ds["aaa"])) == 4


def test_horizon_days_spaces_dates_but_keeps_them_distinct():
    ds = build_synthetic_dataset(_real(), writes_per_keyword=10, horizon_days=100,
                                 novelty_rate=0.5, seed=1)
    dates = [i.date for i in ds["aaa"]]
    assert len(set(dates)) == len(dates) == 10
    assert dates == sorted(dates)  # chronological


def test_horizon_days_smaller_than_writes_is_rejected():
    # n distinct daily dates cannot fit in fewer than n days — must raise, not
    # silently overshoot the requested horizon (regression: round() -> stride 0).
    import pytest
    with pytest.raises(ValueError, match="cannot place"):
        build_synthetic_dataset(_real(), writes_per_keyword=80, horizon_days=30,
                                novelty_rate=0.5, seed=1)


def test_horizon_days_span_stays_within_requested_bound():
    ds = build_synthetic_dataset(_real(), writes_per_keyword=10, horizon_days=90,
                                 novelty_rate=0.5, seed=1)
    dates = [i.date for i in ds["aaa"]]
    span = (_iso(dates[-1]) - _iso(dates[0])).days
    assert span <= 90 and len(set(dates)) == 10


def _iso(s):
    from datetime import date
    return date(*(int(x) for x in s.split("-")))


def test_build_run_inputs_yields_probes_with_survival_split():
    # Over a long consecutive timeline, early facts fall outside recent_days and
    # become 'old' → the probe set has survival probes to measure.
    dataset, probes = build_run_inputs(
        _real(), recent_days=5, writes_per_keyword=30, novelty_rate=1.0, seed=1)
    assert dataset["aaa"] and probes
    assert any(p.category == "old" for p in probes)
    assert any(p.category == "recent" for p in probes)
