from app.config import Settings, get_settings


def test_hybrid_settings_default_on():
    s = Settings()
    assert s.enable_hybrid_search is True  # GA'd 2026-07-04 (gap-closure S1)
    assert s.embedding_provider == "local"
    assert s.embedding_model == "BAAI/bge-small-en-v1.5"
    assert s.embedding_dim == 384
    assert s.embed_poll_seconds == 5.0
    assert s.embed_batch_size == 32
    assert s.candidate_k == 20
    assert s.near_duplicate_threshold == 0.80
    assert s.stale_report_days == 90


def test_admin_session_secret_defaults_to_none(monkeypatch) -> None:
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    get_settings.cache_clear()
    assert get_settings().admin_session_secret is None


def test_admin_session_secret_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "s3cr3t")
    get_settings.cache_clear()
    assert get_settings().admin_session_secret == "s3cr3t"
    get_settings.cache_clear()


def test_forwarded_allow_ips_defaults_to_private_ranges(monkeypatch) -> None:
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    get_settings.cache_clear()
    value = get_settings().forwarded_allow_ips
    # Loopback (dev, no proxy) + the private ranges Docker assigns to the Caddy
    # network, so the proxy peer is trusted while the forwarded client is not.
    assert "127.0.0.1" in value
    assert "10.0.0.0/8" in value
    assert "172.16.0.0/12" in value
    assert "192.168.0.0/16" in value
    get_settings.cache_clear()


def test_forwarded_allow_ips_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "203.0.113.7")
    get_settings.cache_clear()
    assert get_settings().forwarded_allow_ips == "203.0.113.7"
    get_settings.cache_clear()


def test_entry_token_hard_cap_default() -> None:
    from app.config import Settings

    settings = Settings()
    assert settings.entry_token_cap == 2000  # soft cap unchanged
    assert settings.entry_token_hard_cap == 10000  # new hard cap
    assert settings.entry_token_hard_cap > settings.entry_token_cap
