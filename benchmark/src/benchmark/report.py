"""Turn metrics.json into a comparative table and the footprint-over-time chart."""
from __future__ import annotations

import json
from collections import defaultdict


def summarize(rows: list[dict]) -> str:
    final: dict[str, dict] = {}
    for r in rows:
        if r["adapter"] not in final or r["day"] >= final[r["adapter"]]["day"]:
            final[r["adapter"]] = r
    header = f"{'system':<12} {'footprint':>10} {'recall':>7} {'survival':>9} {'ret_tok':>8} {'write_tok':>10}"
    lines = [header, "-" * len(header)]
    for name, r in sorted(final.items()):
        lines.append(
            f"{name:<12} {r['footprint_tokens']:>10} {r['recall']:>7.2f} "
            f"{r['survival_recall']:>9.2f} {r['avg_retrieve_tokens']:>8.0f} "
            f"{r['write_tokens_cumulative']:>10}"
        )
    return "\n".join(lines)


def render_footprint_chart(rows: list[dict], out_path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    series: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for r in rows:
        series[r["adapter"]].append((r["day"], r["footprint_tokens"]))

    fig, ax = plt.subplots(figsize=(9, 5))
    for name, pts in sorted(series.items()):
        pts.sort()
        xs, ys = zip(*pts)
        ax.plot(xs, ys, marker="o", label=name)
    ax.set_xlabel("simulated day")
    ax.set_ylabel("context footprint (tokens)")
    ax.set_title("Context footprint over time")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--metrics", default="results/metrics.json")
    ap.add_argument("--chart", default="results/footprint.png")
    args = ap.parse_args()
    with open(args.metrics, encoding="utf-8") as fh:
        rows = json.load(fh)
    print(summarize(rows))
    render_footprint_chart(rows, args.chart)
    print(f"\nchart -> {args.chart}")


if __name__ == "__main__":
    main()
