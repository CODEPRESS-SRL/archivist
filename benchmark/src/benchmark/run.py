"""Longitudinal simulation loop: replay the dataset day-by-day through every
adapter, snapshot footprint + probes at monthly checkpoints."""
from __future__ import annotations

import json

from benchmark.probes import Probe, evaluate


def _timeline(dataset: dict) -> list[str]:
    dates = sorted({n.date for items in dataset.values() for n in items})
    return dates


async def simulate(dataset, adapters, probes: list[Probe], checkpoint_every: int = 30) -> list[dict]:
    for a in adapters:
        await a.reset()
    dates = _timeline(dataset)
    by_asset_date = {(n.asset, n.date): n for items in dataset.values() for n in items}
    write_cost: dict[str, int] = {a.name: 0 for a in adapters}
    rows: list[dict] = []

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
        if not is_checkpoint:
            continue
        for a in adapters:
            fp = await a.context_footprint()
            rep = await evaluate(a, probes, as_of_date=date)
            rows.append({
                "adapter": a.name,
                "day": i + 1,
                "date": date,
                "footprint_tokens": fp.startup_tokens,
                "recall": rep.recall,
                "survival_recall": rep.survival_recall,
                "avg_retrieve_tokens": rep.avg_retrieve_tokens,
                "write_tokens_cumulative": write_cost[a.name],
            })
    return rows


async def main() -> None:
    import argparse
    import os

    from benchmark.adapters.archivist import ArchivistAdapter, make_mcp_caller
    from benchmark.adapters.claude_mem import ClaudeMemAdapter, make_claude_mem_caller
    from benchmark.adapters.rag_wiki import RagWikiAdapter
    from benchmark.engine import AnthropicClient, WriteEngine
    from benchmark.extract import load_dataset
    from benchmark.probes import load_probes

    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="data/dataset.jsonl")
    ap.add_argument("--probes", default="data/probes.json")
    ap.add_argument("--out", default="results/metrics.json")
    ap.add_argument("--checkpoint-every", type=int, default=30)
    args = ap.parse_args()

    os.makedirs("results", exist_ok=True)

    dataset = load_dataset(args.dataset)
    probes = load_probes(args.probes)
    engine = WriteEngine(AnthropicClient())

    arch_call, arch_close = make_mcp_caller(os.environ["ARCHIVIST_MCP_URL"], os.environ["ARCHIVIST_API_KEY"])
    cm_call, cm_close = make_claude_mem_caller()
    adapters = [
        ArchivistAdapter(call_tool=arch_call, engine=engine, project="benchmark-marketsgazette"),
        ClaudeMemAdapter(call_tool=cm_call, engine=engine, tag="archivist-benchmark"),
        RagWikiAdapter(db_path="results/wiki.db"),
    ]
    try:
        rows = await simulate(dataset, adapters, probes, checkpoint_every=args.checkpoint_every)
    finally:
        await arch_close()
        await cm_close()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
