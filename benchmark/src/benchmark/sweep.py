"""Synthetic long-horizon λ-sweep: run the benchmark at several novelty rates so
the footprint trajectory can be read as a function of how fast real novelty is
consumed. Each λ uses its OWN Archivist project namespace (so keywords never mix
across runs) and writes its metrics before the next λ starts — a crash mid-sweep
still leaves every completed λ on disk.

Integration entrypoint (needs the live server + Gemini); the pure pieces it
composes — `synth.build_run_inputs`, `report.render_lambda_footprint_chart` —
are unit-tested. Runs ~2,400 LLM writes total; confirm cost before launching.
"""
from __future__ import annotations

import asyncio
import json
import os

_LAMBDAS = (0.15, 0.5, 0.85)


def _tag(lam: float) -> str:
    return f"{int(round(lam * 100)):02d}"


async def run_sweep(lambdas=_LAMBDAS, *, writes_per_keyword=80, revision_rate=0.0,
                    horizon_days=None, seed=0, recent_days=14, checkpoint_every=30,
                    dataset_path="data/dataset.jsonl", out_dir="results/synth"):
    from benchmark.extract import load_dataset
    from benchmark.run import run_and_write
    from benchmark.synth import build_run_inputs

    real = load_dataset(dataset_path)
    os.makedirs(out_dir, exist_ok=True)
    runs: dict[str, list[dict]] = {}
    for lam in lambdas:
        dataset, probes = build_run_inputs(
            real, recent_days=recent_days, writes_per_keyword=writes_per_keyword,
            novelty_rate=lam, revision_rate=revision_rate,
            horizon_days=horizon_days, seed=seed,
        )
        out = os.path.join(out_dir, f"metrics_lambda{lam}.json")
        project = f"benchmark-mg-l{_tag(lam)}"
        print(f"[sweep] lambda={lam} project={project} -> {out}", flush=True)
        try:
            rows = await run_and_write(dataset, probes, project=project, out=out,
                                       checkpoint_every=checkpoint_every)
            runs[str(lam)] = rows
        except Exception as e:  # noqa: BLE001 — one λ dying must not lose the others
            print(f"[sweep] lambda={lam} FAILED: {type(e).__name__}: {e}", flush=True)
    return runs


async def main() -> None:
    import argparse

    from benchmark.report import render_lambda_footprint_chart, summarize

    ap = argparse.ArgumentParser()
    ap.add_argument("--writes-per-keyword", type=int, default=80)
    ap.add_argument("--revision-rate", type=float, default=0.0)
    ap.add_argument("--synth-horizon-days", type=int, default=None)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--recent-days", type=int, default=14)
    ap.add_argument("--checkpoint-every", type=int, default=30)
    ap.add_argument("--out-dir", default="results/synth")
    ap.add_argument("--lambdas", default="0.15,0.5,0.85")
    args = ap.parse_args()

    lambdas = tuple(float(x) for x in args.lambdas.split(","))
    runs = await run_sweep(
        lambdas, writes_per_keyword=args.writes_per_keyword, revision_rate=args.revision_rate,
        horizon_days=args.synth_horizon_days, seed=args.seed, recent_days=args.recent_days,
        checkpoint_every=args.checkpoint_every, out_dir=args.out_dir,
    )
    for lam, rows in runs.items():
        print(f"\n=== lambda={lam} (SYNTHETIC: real corpus resampled) ===")
        print(summarize(rows))

    chart = os.path.join(args.out_dir, "footprint_by_lambda.png")
    render_lambda_footprint_chart(runs, chart)
    print(f"\ncombined chart -> {chart}")

    with open(os.path.join(args.out_dir, "sweep.json"), "w", encoding="utf-8") as fh:
        json.dump(runs, fh, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
