"""Synthetic long-horizon dataset: resample REAL articles over a synthetic
timeline so per-keyword depth is deep enough for compaction to fire dozens of
times per keyword — the trajectory the real 15-day window could not show.

TRUTHFULNESS: nothing is fabricated. Every emitted item IS a real article (real
``body`` + real ``facts``); only the *date* and the *arrival pattern* are
synthetic. Per asset, each new day draws from three buckets under a seeded RNG:

- **Restatement** (prob ``1 - lambda - rho``): re-emit a recently-used real
  article. Compaction must collapse these — this is the redundancy H1 relies on.
- **Revision** (prob ``rho``): emit a not-yet-used real article of the SAME asset
  that shares a fact ``type`` with an already-emitted one (a new real value for a
  known metric → supersession). Values are always real.
- **Novelty** (prob ``lambda``): advance to a not-yet-used real article. When the
  finite real pool is exhausted, novelty saturates → the footprint plateaus (the
  honest behaviour; we cannot show unbounded novelty without inventing facts).

Determinism: a single ``random.Random(seed)`` drives every choice; there is no
wall-clock or global-random use, so runs are reproducible (matches the harness's
existing no-time-no-global-random rule).
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from benchmark.types import News

_START_DATE = "2026-08-01"          # after the real corpus max (2026-07-03)
_RESTATE_WINDOW = 10                # restatements sample from the last N emitted


def _dates(n: int, horizon_days: int | None, start: str) -> list[str]:
    """n distinct, chronological ISO dates. Consecutive by default; when
    horizon_days is given, spread across it with an integer stride (floor, not
    round — round(horizon_days/n) can hit 0 and silently overshoot the horizon).
    n distinct daily dates need at least n days, so a smaller horizon is rejected
    rather than silently drifting."""
    y, m, d = (int(x) for x in start.split("-"))
    d0 = date(y, m, d)
    if not horizon_days or not n:
        stride = 1
    elif horizon_days < n:
        raise ValueError(
            f"horizon_days={horizon_days} < writes_per_keyword={n}: cannot place "
            f"{n} distinct daily dates within {horizon_days} days"
        )
    else:
        stride = horizon_days // n
    return [(d0 + timedelta(days=i * stride)).isoformat() for i in range(n)]


def _emit(asset: str, src: News, iso_date: str) -> News:
    """A real article re-dated onto the synthetic timeline (body/facts untouched)."""
    return News(asset=asset, date=iso_date, title=src.title, body=src.body, facts=src.facts)


def _fact_types(article: News) -> set[str]:
    return {f.type for f in article.facts}


def build_synthetic_dataset(
    real: dict[str, list[News]],
    *,
    writes_per_keyword: int = 80,
    novelty_rate: float,
    revision_rate: float = 0.0,
    horizon_days: int | None = None,
    seed: int = 0,
    start_date: str = _START_DATE,
) -> dict[str, list[News]]:
    """Build a ``dict[asset, list[News]]`` — the exact shape ``simulate()`` loads —
    with ``writes_per_keyword`` items per asset (one per distinct synthetic date).

    Assets are processed in sorted order and each draws from its own RNG stream
    (``seed`` + asset name hash-free offset via index) so the mixture is stable
    and independent per keyword.
    """
    out: dict[str, list[News]] = {}
    for a_idx, asset in enumerate(sorted(real)):
        pool = real[asset]
        if not pool:
            out[asset] = []
            continue
        rng = random.Random(f"{seed}:{a_idx}")
        dates = _dates(writes_per_keyword, horizon_days, start_date)

        emitted_idx: list[int] = []          # pool indices already emitted (order of use)
        used: set[int] = set()               # pool indices consumed as fresh (novelty/revision)
        seen_types: set[str] = set()
        items: list[News] = []

        for i, iso_date in enumerate(dates):
            if i == 0:
                chosen = 0                    # first item is always the pool's first article
            else:
                u = rng.random()
                chosen = None
                if u < novelty_rate:
                    chosen = _next_unused(pool, used)          # novelty
                elif u < novelty_rate + revision_rate:
                    chosen = _next_revision(pool, used, seen_types)  # revision
                if chosen is None:            # restatement (or novelty/revision unavailable)
                    chosen = _restate(rng, emitted_idx)

            used.add(chosen)
            emitted_idx.append(chosen)
            seen_types |= _fact_types(pool[chosen])
            items.append(_emit(asset, pool[chosen], iso_date))

        out[asset] = items
    return out


def build_run_inputs(real, *, recent_days: int = 14, **synth_kwargs):
    """Convenience: build the synthetic dataset AND its auto-derived probe set in
    one call (probes regenerate from the synthetic timeline, so month-1 facts
    become 'old' → survival). Returns ``(dataset, probes)``."""
    from benchmark.probes import build_probes_from_dataset

    dataset = build_synthetic_dataset(real, **synth_kwargs)
    probes = build_probes_from_dataset(dataset, recent_days=recent_days)
    return dataset, probes


def _next_unused(pool: list[News], used: set[int]) -> int | None:
    """Lowest pool index not yet consumed — deterministic novelty order."""
    for j in range(len(pool)):
        if j not in used:
            return j
    return None


def _next_revision(pool: list[News], used: set[int], seen_types: set[str]) -> int | None:
    """Lowest unused article that shares a fact type with something already emitted —
    a new real value for a known metric. Falls back to None (→ restatement)."""
    for j in range(len(pool)):
        if j not in used and (_fact_types(pool[j]) & seen_types):
            return j
    return None


def _restate(rng: random.Random, emitted_idx: list[int]) -> int:
    """Repeat one of the most recently emitted articles."""
    window = emitted_idx[-_RESTATE_WINDOW:]
    return rng.choice(window)
