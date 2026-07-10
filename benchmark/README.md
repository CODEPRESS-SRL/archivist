# Archivist Memory Benchmark

Longitudinal stress-test: after ~4 months of daily news on 5 assets, which
memory system stays dense, complete and *bounded* — Archivist, claude-mem, or
a RAG-wiki? See the design spec at
`../docs/superpowers/specs/2026-07-10-memory-benchmark-design.md`.

## Prerequisites
- `uv`, Python 3.12
- Archivist dev stack running (`make up` in the repo root) + a benchmark API key
- `ANTHROPIC_API_KEY` for the write-engine (Haiku 4.5) — **budget authorised**
- marketsgazette DB DSN (`MARKETSGAZETTE_DSN`) for extraction only

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
