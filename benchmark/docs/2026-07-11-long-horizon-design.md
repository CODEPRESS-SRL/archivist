# Long-horizon benchmark extension — design

**Date:** 2026-07-11 · **Status:** approved, implementing · **Scope:** `benchmark/`

## Problem

The real 15-day run (`results/metrics.json`, committed `cee77f6`) could **not**
test H1 — *"Archivist stays bounded via merge-on-read compaction while others
grow"* — because per-keyword depth was too shallow: compaction fired only ~2×
per keyword (threshold is `>3` fragments). Over that window Archivist's footprint
actually **grew** (1106→2852). We need a longer horizon where compaction fires
dozens of times per keyword so the footprint *trajectory* becomes observable.

## Data ceiling (measured, backend_news 2026-07-11)

- Span 2026-05-21 → 2026-07-03; only **27 distinct asset-days**, concentrated in
  the last ~2 weeks.
- **Per-ticker depth is irreducibly shallow**: best-covered ticker (DXY) = 13
  distinct days; most 7-11; MU = 49 rows but only 4 days.

Therefore:
- **(a) widen the real news window → dead end.** Nothing to widen; per-keyword
  timeline stays 4-13 days, compaction still barely fires.
- **(c) lower the compaction threshold → not a test of H1.** Adds no writes/time;
  only changes *merge frequency*. Kept as an **optional ablation**, not the core.
- **(b) simulate months → the only path to the horizon.** Chosen.

## Honest claim (scope boundary)

Real per-asset fact pools are finite and shallow, so we **cannot** truthfully
show *unbounded sub-linear growth* without fabricating facts. What the real
corpus **can** demonstrate:

> When genuine novelty about an asset is exhausted, Archivist's footprint goes to
> a **plateau** (compaction collapses the ongoing restatements/revisions), while
> **raw storage grows linearly** and **claude-mem plateaus only by dropping old
> facts** (survival→0). The novelty rate λ controls how fast novelty is consumed
> → different approach curves, same qualitative contrast.

Deliverable = the footprint↔recall↔survival **trajectory** across a λ sweep,
explicitly labelled synthetic — not a ✅/❌ verdict on the original H1.

## Synthesis fidelity: resample **real** articles (no fabrication)

The long dataset is a **repetition-resampling of the real articles** over a
synthetic timeline. Every item `(asset, synthetic_date)` **is** a real article
(real `body` + real `facts`); only the *date* and *arrival pattern* are
synthetic. Per asset, each new day draws from three buckets under a seeded RNG:

- **Restatement** (`1-λ-ρ`): re-emit a recently-used real article → compaction
  must collapse it (the redundancy H1 relies on).
- **Revision** (`ρ`): emit another real article of the **same asset** sharing a
  fact `type` (e.g. a different real `%`) → supersession; values always real.
- **Novelty** (`λ`): advance to a not-yet-used real article of the asset. When
  the pool is exhausted, novelty saturates → plateau (the honest behaviour).

No LLM-invented text or numbers. "Months" are synthetic in *date* only.

## Components (surgical; reuses the existing harness)

- **`synth.py`** (new): `build_synthetic_dataset(real, *, horizon_days,
  writes_per_keyword=80, novelty_rate, revision_rate, seed) ->
  dict[asset, list[News]]`. Emits **exactly** the shape `simulate()` already
  consumes → **no change** to `run.py`, adapters, or `probes.py`. One item per
  **distinct synthetic date** per asset (simulate is last-wins per (asset,date),
  so writes-per-keyword == distinct synthetic dates per asset). Probes
  auto-regenerate from the synthetic dataset (`build_probes_from_dataset`);
  month-1 facts become "old" → survival.
- **Entrypoint** `run.py --synth …` (flags: `--synth-horizon-days`,
  `--writes-per-keyword`, `--novelty-rate`, `--revision-rate`, `--seed`,
  `--out`). A small driver sweeps **λ ∈ {0.15, 0.5, 0.85}** and writes combined
  chart + report.
- **Separate artifacts**: `results/synth/metrics_lambda{λ}.json`,
  `results/synth/footprint_by_lambda.png`, report. **Never** overwrites the real
  `results/metrics.json`. Report header labels it SYNTHETIC (real corpus
  resampled, every fact real, dates/arrival synthetic, λ=…).
- **Ablation (c), optional/off by default**: `ArchivistAdapter(compact_threshold=K)`
  forces more-eager client-side compaction (K=2) without touching prod, to show
  the footprint↔survival↔cost tradeoff.

## Fairness & determinism

- The **same** synthetic stream feeds all 3 adapters (claude-mem plateaus at its
  50-obs cap, rag stays 0-footprint — as before).
- `random.Random(seed)`, no wall-clock/`Date.now` → runs are reproducible
  (matches the harness's existing no-time-no-global-random rule).

## Tests (BLOCKING)

`test_synth.py`: (a) determinism — same seed ⇒ identical dataset; (b) λ
monotonicity — higher λ ⇒ more distinct real articles consumed; (c) restatement
actually repeats (fragments accumulate so compaction can fire); (d) novelty
saturates at pool exhaustion; (e) output shape loads through the existing path.
Plus the 44 existing tests stay green. If the ablation lands, one test for
`compact_threshold`.

## Cost / time

~6 months, ~80 writes/keyword, 10 keywords, sweep ×3 λ ⇒ ~2,400 LLM writes,
**~$0.45 Gemini**, **~4-6h** wall-clock with the RPM throttle. Runs one λ at a
time; each λ writes its metrics before the next.
