"""Application configuration.

Every value is read from the environment (see ``.env.example``). The
defaults mirror the constants table in ``MVP_WORKBOOK.md`` — do not change
them here without updating the workbook. Environment variable names are the
upper-cased field names (e.g. ``ENTRY_TOKEN_CAP`` -> ``entry_token_cap``).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Single source for the embedding dimension on the app side (bge-small-en-v1.5
# output = 384). Migration 0002 creates the matching ``vector(N)`` column; the
# two are pinned together by ``test_embedding_column_dim_matches_settings`` so a
# change here that isn't carried into a new migration fails the suite.
EMBEDDING_DIM = 384


class Settings(BaseSettings):
    """Runtime settings, populated from the environment."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://archivist:archivist@localhost:5432/archivist"

    # Token caps
    entry_token_cap: int = 2000  # SOFT cap — over this the write is flagged, not rejected
    entry_token_hard_cap: int = 10000  # HARD cap — writes above this are rejected
    fragment_token_cap: int = 500

    # Compaction thresholds
    compaction_fragment_threshold: int = 3
    compaction_token_ratio: float = 0.66
    hard_limit_fragments: int = 10

    # Search / rate limiting
    search_limit_default: int = 5
    rate_limit_per_minute: int = 120

    # Admin console (server-rendered /admin). If unset, an ephemeral secret is
    # generated at startup and sessions reset on restart — set this in prod.
    admin_session_secret: str | None = None

    # Trusted proxy hosts for X-Forwarded-For/-Proto (uvicorn's
    # ProxyHeadersMiddleware, wired in create_app). Behind Caddy the immediate
    # peer is a Docker-network address, so the private ranges are trusted while
    # the forwarded public client is not — the middleware then keys the admin
    # login throttle on the real client IP, not the shared proxy address. Do NOT
    # set this to "*" in prod: that trusts the left-most (client-supplied) entry
    # and lets a spoofed X-Forwarded-For evade the per-IP throttle.
    forwarded_allow_ips: str = "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"

    # Hybrid retrieval — GA'd ON by default (gap-closure S1, 2026-07-04). The
    # pilot's GA gate is retired: failure modes were already observed and local
    # embeddings cost ~nothing. Set ENABLE_HYBRID_SEARCH=false to fall back to
    # pure lexical search.
    enable_hybrid_search: bool = True
    embedding_provider: str = "local"  # "local" | "external"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = EMBEDDING_DIM  # must match the vector(N) column in migration 0002
    embed_poll_seconds: float = 5.0
    embed_batch_size: int = 32
    candidate_k: int = 20  # per-retriever candidate depth fed into RRF

    # Gap-closure S2/S4: cosine similarity floor for near_duplicates on create
    # and for the admin consolidation report; age floor for the stale report.
    near_duplicate_threshold: float = 0.80
    stale_report_days: int = 90


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached ``Settings`` instance."""
    return Settings()
