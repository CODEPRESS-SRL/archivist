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
    # Adapted to the real backend_news schema (2026-07-10): there is no asset FK
    # on `news` (the `assets`/`news_assets` tables are unpopulated). The per-news
    # asset associations live in `ai_data->'assets'` — a JSON array of
    # {name, ticker, country, sentiment}. We explode that, rank tickers by news
    # count, take the top 10, and emit one row per (news, top-10 ticker). Title
    # and content are the ORIGINAL English article text (only ai_data.* is Italian).
    return """
        WITH exploded AS (
            SELECT a->>'ticker' AS ticker,
                   n.date::date  AS d,
                   n.title       AS title,
                   n.content     AS body
            FROM news n,
                 jsonb_array_elements(n.ai_data->'assets') a
            WHERE n.ai_data ? 'assets'
              AND a->>'ticker' IS NOT NULL
              AND n.title IS NOT NULL
              AND n.content IS NOT NULL
        ),
        top AS (
            SELECT ticker FROM exploded
            GROUP BY ticker ORDER BY COUNT(*) DESC LIMIT 10
        )
        SELECT ticker, d, title, body
        FROM exploded
        WHERE ticker IN (SELECT ticker FROM top)
        ORDER BY ticker, d
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
