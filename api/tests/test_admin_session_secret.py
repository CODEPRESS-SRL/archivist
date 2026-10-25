"""Startup warnings for an empty, unset, or short ADMIN_SESSION_SECRET."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator

import pytest

from app import main
from app.config import get_settings


@pytest.fixture
def build_app(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Iterator[Callable[[str | None], list[str]]]:
    """Build the app with a given secret and return the WARNING messages it logged.

    ``configure_logging`` is disabled because it clears the root handlers,
    which would detach caplog's handler.
    """
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    caplog.set_level(logging.WARNING, logger="archivist")

    def _build(secret: str | None) -> list[str]:
        if secret is None:
            monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
        else:
            monkeypatch.setenv("ADMIN_SESSION_SECRET", secret)
        get_settings.cache_clear()
        caplog.clear()
        main.create_app()
        return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]

    yield _build
    get_settings.cache_clear()


@pytest.mark.parametrize("secret", [None, ""])
def test_unset_or_empty_secret_warns_ephemeral(build_app, secret: str | None) -> None:
    warnings = build_app(secret)
    assert any("ADMIN_SESSION_SECRET is unset or empty" in w for w in warnings)


def test_short_secret_warns_with_generation_hint_and_still_starts(build_app) -> None:
    short = "x" * (main.MIN_ADMIN_SESSION_SECRET_CHARS - 1)
    warnings = build_app(short)
    hint = [w for w in warnings if "shorter than 32 characters" in w]
    assert len(hint) == 1
    assert "at least 32 random characters" in hint[0]
    assert "secrets.token_urlsafe(32)" in hint[0]
    assert short not in hint[0]
    assert not any("ephemeral" in w for w in warnings)


def test_strong_secret_logs_no_secret_warning(build_app) -> None:
    warnings = build_app("x" * main.MIN_ADMIN_SESSION_SECRET_CHARS)
    assert not any("ADMIN_SESSION_SECRET" in w for w in warnings)
