"""Longitudinal simulation loop: replay the dataset day-by-day through every
adapter, snapshot footprint + probes at monthly checkpoints."""
from __future__ import annotations

import json
import sys

from benchmark.engine import FreeTierBudgetExceeded
from benchmark.probes import Probe, evaluate


def _timeline(dataset: dict) -> list[str]:
    dates = sorted({n.date for items in dataset.values() for n in items})
    return dates


async def _record_checkpoint(rows, adapters, probes, i, date, write_cost, *, budget_stopped=False):
    # Checkpoints cost ZERO LLM calls: footprint + retrieve use MCP/HTTP/local
    # embeddings, never the write-engine. Safe to record even after a budget stop.
    for a in adapters:
        fp = await a.context_footprint()
        rep = await evaluate(a, probes, as_of_date=date)
        row = {
            "adapter": a.name,
            "day": i + 1,
            "date": date,
            "footprint_tokens": fp.startup_tokens,
            "recall": rep.recall,
            "survival_recall": rep.survival_recall,
            "avg_retrieve_tokens": rep.avg_retrieve_tokens,
            "write_tokens_cumulative": write_cost[a.name],
        }
        if budget_stopped:
            row["budget_stopped"] = True
        rows.append(row)


async def simulate(dataset, adapters, probes: list[Probe], checkpoint_every: int = 30) -> list[dict]:
    for a in adapters:
        await a.reset()
    dates = _timeline(dataset)
    by_asset_date = {(n.asset, n.date): n for items in dataset.values() for n in items}
    write_cost: dict[str, int] = {a.name: 0 for a in adapters}
    rows: list[dict] = []

    i = date = None
    try:
        for i, date in enumerate(dates):
            for asset in dataset:
                news = by_asset_date.get((asset, date))
                if news is None:
                    continue
                for a in adapters:
                    wr = await a.write(news)
                    write_cost[a.name] += wr.llm_tokens
                    comp = await a.maybe_compact(asset)
                    if comp:
                        write_cost[a.name] += comp.llm_tokens

            is_checkpoint = (i + 1) % checkpoint_every == 0 or i == len(dates) - 1
            if is_checkpoint:
                await _record_checkpoint(rows, adapters, probes, i, date, write_cost)
    except FreeTierBudgetExceeded as e:
        # Free-tier RPD guard tripped mid-run: capture a final checkpoint at the
        # furthest state reached (checkpoints are LLM-free) so partial results
        # still get written, then stop cleanly.
        print(f"[benchmark] {e}", file=sys.stderr)
        print(f"[benchmark] recording a final (truncated) checkpoint at day {i + 1} ({date})",
              file=sys.stderr)
        await _record_checkpoint(rows, adapters, probes, i, date, write_cost, budget_stopped=True)

    return rows


async def run_and_write(dataset, probes, *, project, out, checkpoint_every):
    """Wire the three real adapters against `project`, run the simulation, and
    persist the rows to `out`. Shared by the real run (`main`) and the synthetic
    λ-sweep (`sweep.py`). Adapter imports are lazy so unit tests never pull in
    the MCP/embedding stacks."""
    import os

    from benchmark.adapters.archivist import ArchivistAdapter, make_mcp_caller
    from benchmark.adapters.claude_mem import ClaudeMemAdapter, make_claude_mem_caller
    from benchmark.adapters.rag_wiki import RagWikiAdapter
    from benchmark.engine import WriteEngine, make_llm_client

    engine = WriteEngine(make_llm_client())
    arch_call, arch_close = make_mcp_caller(os.environ["ARCHIVIST_MCP_URL"], os.environ["ARCHIVIST_API_KEY"])
    cm_call, cm_close = make_claude_mem_caller()
    adapters = [
        ArchivistAdapter(call_tool=arch_call, engine=engine, project=project),
        ClaudeMemAdapter(call_tool=cm_call, engine=engine, tag="archivist-benchmark"),
        RagWikiAdapter(db_path="results/wiki.db"),
    ]
    rows = None
    try:
        rows = await simulate(dataset, adapters, probes, checkpoint_every=checkpoint_every)
    finally:
        # Persist BEFORE teardown. The streamable-HTTP session can raise
        # CancelledError while closing (an idle/keep-alive cancel scope fires),
        # which would otherwise discard a fully-completed run's results. Close is
        # then best-effort — teardown errors on a session we're discarding are moot.
        if rows is not None:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, indent=2)
        for close in (arch_close, cm_close):
            try:
                await close()
            except BaseException:  # noqa: BLE001 — discard teardown noise, keep results
                pass
    return rows


async def main() -> None:
    import argparse
    import os

    from benchmark.extract import load_dataset
    from benchmark.probes import load_probes
    from benchmark.synth import build_run_inputs

    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="data/dataset.jsonl")
    ap.add_argument("--probes", default="data/probes.json")
    ap.add_argument("--out", default="results/metrics.json")
    ap.add_argument("--project", default="benchmark")
    ap.add_argument("--checkpoint-every", type=int, default=30)
    # Synthetic long-horizon mode: resample the real dataset over a long timeline
    # so compaction fires many times per keyword (see docs/2026-07-11-*).
    ap.add_argument("--synth", action="store_true",
                    help="build a synthetic long-horizon dataset from --dataset instead of --probes")
    ap.add_argument("--writes-per-keyword", type=int, default=80)
    ap.add_argument("--novelty-rate", type=float, default=0.5)
    ap.add_argument("--revision-rate", type=float, default=0.0)
    ap.add_argument("--synth-horizon-days", type=int, default=None)
    ap.add_argument("--synth-seed", type=int, default=0)
    ap.add_argument("--recent-days", type=int, default=14)
    args = ap.parse_args()

    os.makedirs("results", exist_ok=True)

    if args.synth:
        dataset, probes = build_run_inputs(
            load_dataset(args.dataset),
            recent_days=args.recent_days,
            writes_per_keyword=args.writes_per_keyword,
            novelty_rate=args.novelty_rate,
            revision_rate=args.revision_rate,
            horizon_days=args.synth_horizon_days,
            seed=args.synth_seed,
        )
    else:
        dataset = load_dataset(args.dataset)
        probes = load_probes(args.probes)

    await run_and_write(dataset, probes, project=args.project, out=args.out,
                        checkpoint_every=args.checkpoint_every)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
