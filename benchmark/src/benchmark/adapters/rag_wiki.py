"""RAG-wiki: honest minimal vector store (sqlite-vec + fastembed).

Writes raw news chunks (no LLM compression). Startup footprint is 0 —
the cost is paid per query in retrieve(). k = top-5 chunks by cosine.
fastembed is onnxruntime-based (no torch) and L2-normalizes by default.
"""
from __future__ import annotations

import sqlite3
import struct

import sqlite_vec

from benchmark.tokens import count_tokens
from benchmark.types import CompactResult, FootprintSnapshot, News, RetrieveResult, WriteResult

_MODEL = "BAAI/bge-small-en-v1.5"  # 384-dim, normalized
_DIM = 384
_K = 5


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


class RagWikiAdapter:
    name = "rag-wiki"

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._model = None  # lazy: loaded on first embed

    def _embed(self, text: str) -> list[float]:
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(_MODEL)
        return list(self._model.embed([text]))[0].tolist()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    async def reset(self) -> None:
        conn = self._connect()
        conn.execute("DROP TABLE IF EXISTS chunks")
        conn.execute(f"CREATE VIRTUAL TABLE chunks USING vec0(embedding float[{_DIM}], text TEXT)")
        conn.commit()
        conn.close()

    async def write(self, news: News) -> WriteResult:
        text = f"[{news.date}] {news.title}\n{news.body}"
        vec = self._embed(text)
        conn = self._connect()
        conn.execute("INSERT INTO chunks(embedding, text) VALUES (?, ?)", (_pack(vec), text))
        conn.commit()
        conn.close()
        return WriteResult(llm_tokens=0)  # no LLM in the write path

    async def maybe_compact(self, asset: str) -> CompactResult | None:
        return None  # RAG never compacts

    async def context_footprint(self) -> FootprintSnapshot:
        return FootprintSnapshot(startup_tokens=0, detail={"mode": "retrieval-only"})

    async def retrieve(self, query: str) -> RetrieveResult:
        vec = self._embed(query)
        conn = self._connect()
        rows = conn.execute(
            "SELECT text FROM chunks WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (_pack(vec), _K),
        ).fetchall()
        conn.close()
        payload = "\n---\n".join(r[0] for r in rows)
        return RetrieveResult(payload=payload, tokens=count_tokens(payload))
