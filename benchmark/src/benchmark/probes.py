"""Deterministic fact-match probe set and evaluator (no LLM judge)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from benchmark.types import News


@dataclass(frozen=True)
class Probe:
    id: str
    query: str
    pattern: str          # regex searched (case-insensitive) in the retrieved payload
    category: str         # "recent" | "old" | "aggregate"
    available_from: str   # ISO date; probe counts only at/after this checkpoint


@dataclass
class ProbeReport:
    recall: float
    survival_recall: float
    avg_retrieve_tokens: float
    hits: int
    total: int


def load_probes(path: str) -> list[Probe]:
    with open(path, encoding="utf-8") as fh:
        return [Probe(**p) for p in json.load(fh)]


async def evaluate(adapter, probes: list[Probe], as_of_date: str) -> ProbeReport:
    active = [p for p in probes if p.available_from <= as_of_date]
    hits = 0
    old_total = old_hits = 0
    tokens: list[int] = []
    for p in active:
        res = await adapter.retrieve(p.query)
        tokens.append(res.tokens)
        hit = re.search(p.pattern, res.payload or "", re.I) is not None
        hits += int(hit)
        if p.category == "old":
            old_total += 1
            old_hits += int(hit)
    total = len(active)
    return ProbeReport(
        recall=hits / total if total else 0.0,
        survival_recall=old_hits / old_total if old_total else 0.0,
        avg_retrieve_tokens=sum(tokens) / len(tokens) if tokens else 0.0,
        hits=hits,
        total=total,
    )


def build_probes_from_dataset(dataset: dict[str, list[News]], recent_days: int = 30) -> list[Probe]:
    """Auto-derive probes from extracted facts. Review by hand before the real run.

    Builds only from the news that `simulate` actually writes — one per
    (asset, date), last wins — so no probe references a fact from a same-day
    news that never got written (which would depress recall spuriously)."""
    probes: list[Probe] = []
    for asset, items in dataset.items():
        survivors = list({n.date: n for n in items}.values())  # last per date, matches simulate
        last_date = survivors[-1].date if survivors else "1970-01-01"
        for n in survivors:
            for i, f in enumerate(n.facts):
                category = "recent" if n.date >= _shift(last_date, -recent_days) else "old"
                probes.append(Probe(
                    id=f"{asset}-{n.date}-{i}",
                    query=f"{f.type} {asset}",
                    pattern=re.escape(f.value),
                    category=category,
                    available_from=n.date,
                ))
    return probes


def _shift(iso_date: str, days: int) -> str:
    from datetime import date, timedelta
    y, m, d = (int(x) for x in iso_date.split("-"))
    return (date(y, m, d) + timedelta(days=days)).isoformat()


def main() -> None:
    """Generate data/probes.json from an extracted dataset. Review by hand
    afterwards (the whole point of a probe set is that it's trustworthy)."""
    import argparse
    from dataclasses import asdict

    from benchmark.extract import load_dataset

    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="data/dataset.jsonl")
    ap.add_argument("--out", default="data/probes.json")
    ap.add_argument("--recent-days", type=int, default=14,
                    help="facts newer than (last date - N) count as 'recent', older as 'old'")
    args = ap.parse_args()

    dataset = load_dataset(args.dataset)
    probes = build_probes_from_dataset(dataset, recent_days=args.recent_days)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump([asdict(p) for p in probes], fh, indent=2)
    n_old = sum(1 for p in probes if p.category == "old")
    print(f"wrote {len(probes)} probes ({n_old} old / {len(probes) - n_old} recent) -> {args.out}")


if __name__ == "__main__":
    main()
