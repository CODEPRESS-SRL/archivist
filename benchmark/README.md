# Archivist Memory Benchmark

Longitudinal stress-test: after ~4 months of daily news on 5 assets, which
memory system stays dense, complete and *bounded* — Archivist, claude-mem, or
a RAG-wiki?

## Prerequisites
- `uv`, Python 3.12
- Archivist dev stack running (`make up` in the repo root) + a benchmark API key
- `ANTHROPIC_API_KEY` for the write-engine (Haiku 4.5)
- For extraction only: `BENCHMARK_SOURCE_DSN`, the DSN of a Postgres DB of
  daily news items tagged with assets. It needs a `news` table with `date`,
  `title`, `content` and `ai_data` (jsonb, whose `assets` key is an array of
  objects with a `ticker`). See `src/benchmark/extract.py`. Without it, use the
  committed sample data.

## Run
```bash
cd benchmark
uv sync
uv run pytest                              # harness unit tests (no DB, no LLM)
uv run python -m benchmark.extract --out data/dataset.jsonl   # needs DSN
uv run python -m benchmark.run --dataset data/dataset.jsonl --probes data/probes.json
uv run python -m benchmark.report
```
Outputs: `results/metrics.json`, `results/footprint.png`.
