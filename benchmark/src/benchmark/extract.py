"""Extract the top-5 assets' daily news from the marketsgazette DB into JSONL.

The loader (`load_dataset`) is pure and testable on the committed sample.
`main()` connects to the real DB (requires MARKETSGAZETTE_DSN) — run only when
credentials are available. It writes benchmark/data/dataset.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict

from benchmark.facts import extract_facts
from benchmark.types import Fact, News


def load_dataset(path: str) -> dict[str, list[News]]:
    by_asset: dict[str, list[News]] = defaultdict(list)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            facts = tuple(
                Fact(type=f["type"], value=f["value"], unit=f.get("unit"))
                for f in r.get("facts", [])
            )
            by_asset[r["asset"]].append(
                News(asset=r["asset"], date=r["date"], title=r["title"], body=r["body"], facts=facts)
            )
    for asset in by_asset:
        by_asset[asset].sort(key=lambda n: n.date)
    return dict(by_asset)


def _top5_query() -> str:
    # Adjust table/column names during the real extraction after inspecting the schema.
    return """
        WITH top AS (
            SELECT asset_id, COUNT(*) AS n FROM news
            GROUP BY asset_id ORDER BY n DESC LIMIT 5
        )
        SELECT a.ticker, n.published_at::date AS d, n.title, n.body
        FROM news n JOIN asset a ON a.id = n.asset_id
        WHERE n.asset_id IN (SELECT asset_id FROM top)
        ORDER BY a.ticker, d
    """


def main() -> None:
    import psycopg  # imported here so tests never require the driver/DB

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/dataset.jsonl")
    ap.add_argument("--dsn", default=os.environ.get("MARKETSGAZETTE_DSN"))
    args = ap.parse_args()
    if not args.dsn:
        raise SystemExit("MARKETSGAZETTE_DSN not set — cannot reach the real DB")

    with psycopg.connect(args.dsn) as conn, conn.cursor() as cur, open(args.out, "w", encoding="utf-8") as out:
        cur.execute(_top5_query())
        for ticker, d, title, body in cur:
            title, body = title or "", body or ""
            facts = extract_facts(title, body)
            out.write(json.dumps({
                "asset": ticker.lower(), "date": d.isoformat(),
                "title": title, "body": body,
                "facts": [{"type": f.type, "value": f.value, "unit": f.unit} for f in facts],
            }) + "\n")


if __name__ == "__main__":
    main()
