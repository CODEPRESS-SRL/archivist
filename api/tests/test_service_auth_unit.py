"""Service-auth mode on /mcp — the parts that need no database.

Pure functions (secret comparison, header validation) and the middleware
driven directly with an ASGI scope, the team lookup replaced by a stub. The
end-to-end tests with a real Postgres are in ``test_service_auth.py``.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app import auth
from app.auth import (
    AuthContext,
    AuthMiddleware,
    current_key,
    is_service_bearer,
    service_mode_secret,
    validate_team_header,
)
from app.config import SERVICE_SECRET_MIN_LENGTH, Settings, get_settings

SECRET = "s" * 40


# --- config ----------------------------------------------------------------


def test_service_secret_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHIVIST_SERVICE_SECRET", raising=False)
    get_settings.cache_clear()
    assert get_settings().service_secret is None
    get_settings.cache_clear()


def test_service_secret_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHIVIST_SERVICE_SECRET", SECRET)
    get_settings.cache_clear()
    assert get_settings().service_secret == SECRET
    get_settings.cache_clear()


def test_min_length_constant() -> None:
    assert SERVICE_SECRET_MIN_LENGTH == 32
    assert "service_secret" in Settings.model_fields


# --- secret comparison -----------------------------------------------------


def test_mode_off_when_secret_missing_or_short() -> None:
    assert service_mode_secret(None) is None
    assert service_mode_secret("") is None
    assert service_mode_secret("x" * 31) is None
    assert service_mode_secret("x" * 32) == "x" * 32


def test_bearer_matches_only_exact_secret() -> None:
    assert is_service_bearer(SECRET, SECRET) is True
    assert is_service_bearer(SECRET + "x", SECRET) is False
    assert is_service_bearer("x" * 40, SECRET) is False
    assert is_service_bearer("arc_whatever", SECRET) is False


def test_bearer_never_matches_when_mode_off() -> None:
    short = "x" * 31
    assert is_service_bearer(short, short) is False  # secret too short: off
    assert is_service_bearer("anything", None) is False
    assert is_service_bearer("", None) is False


# --- team header validation ------------------------------------------------


@pytest.mark.parametrize("value", ["codepress", "Team With Space", "a" * 64, "àé"])
def test_team_header_accepts_plain_names(value: str) -> None:
    assert validate_team_header(value) == value


@pytest.mark.parametrize(
    "value",
    [None, "", "a" * 65, "x\r\ny", "x\ny", "x\ry", "x\x00y", "x\ty", "x\x7fy"],
)
def test_team_header_rejects_bad_values(value: str | None) -> None:
    assert validate_team_header(value) is None


# --- middleware, DB-free ---------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.reached: AuthContext | None = None
        self.app_called = False

    async def app(self, scope, receive, send) -> None:
        self.app_called = True
        self.reached = current_key.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    @property
    def status(self) -> int:
        return self.sent[0]["status"]


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def _scope(headers: list[tuple[bytes, bytes]]) -> dict:
    return {"type": "http", "path": "/mcp", "method": "POST", "headers": headers}


def _middleware(monkeypatch: pytest.MonkeyPatch, secret: str | None, rec: _Recorder):
    monkeypatch.setattr(get_settings(), "service_secret", secret)
    mw = AuthMiddleware(rec.app)
    lookups: list[str] = []

    async def _team(name: str) -> AuthContext | None:
        lookups.append(name)
        if name == "codepress":
            return AuthContext(key_id=0, owner_name="workspace", team_id=7, team_name=name)
        return None

    async def _key(key_hash: str) -> AuthContext | None:
        lookups.append("key:" + key_hash[:8])
        return None

    monkeypatch.setattr(mw, "_lookup_team", _team)
    monkeypatch.setattr(mw, "_lookup", _key)
    return mw, lookups


def _service_headers(team: bytes | None = b"codepress", secret: str = SECRET):
    headers = [(b"authorization", f"Bearer {secret}".encode())]
    if team is not None:
        headers.append((b"x-archivist-team", team))
    return headers


async def test_secret_and_known_team_reach_app_with_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder()
    mw, _ = _middleware(monkeypatch, SECRET, rec)
    await mw(_scope(_service_headers()), _receive, rec.send)
    assert rec.status == 200
    assert rec.reached == AuthContext(0, "workspace", 7, "codepress")
    assert current_key.get() is None  # reset after the request


async def test_unknown_team_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    mw, _ = _middleware(monkeypatch, SECRET, rec)
    await mw(_scope(_service_headers(b"nope")), _receive, rec.send)
    assert rec.status == 401
    assert rec.app_called is False


@pytest.mark.parametrize(
    "team", [None, b"", b"a" * 65, b"code\r\npress", b"code\npress", b"code\x00press"]
)
async def test_bad_team_header_is_401_without_lookup(
    monkeypatch: pytest.MonkeyPatch, team: bytes | None
) -> None:
    rec = _Recorder()
    mw, lookups = _middleware(monkeypatch, SECRET, rec)
    await mw(_scope(_service_headers(team)), _receive, rec.send)
    assert rec.status == 401
    assert rec.app_called is False
    assert lookups == []  # rejected before any DB access


async def test_repeated_team_header_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    mw, lookups = _middleware(monkeypatch, SECRET, rec)
    headers = _service_headers() + [(b"x-archivist-team", b"codepress")]
    await mw(_scope(headers), _receive, rec.send)
    assert rec.status == 401
    assert lookups == []


async def test_wrong_secret_falls_back_to_key_path(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    mw, lookups = _middleware(monkeypatch, SECRET, rec)
    await mw(_scope(_service_headers(secret="w" * 40)), _receive, rec.send)
    assert rec.status == 401
    assert [entry.split(":")[0] for entry in lookups] == ["key"]  # arc_ path, not team


@pytest.mark.parametrize("secret", [None, "", "x" * 31])
async def test_mode_off_never_opens_with_any_bearer(
    monkeypatch: pytest.MonkeyPatch, secret: str | None
) -> None:
    rec = _Recorder()
    mw, lookups = _middleware(monkeypatch, secret, rec)
    bearer = secret or "anything"
    await mw(_scope(_service_headers(secret=bearer)), _receive, rec.send)
    assert rec.status == 401
    assert rec.app_called is False
    assert [entry.split(":")[0] for entry in lookups] == ["key"]


async def test_service_path_skips_rate_limit_and_last_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder()
    mw, _ = _middleware(monkeypatch, SECRET, rec)
    touched: list[int] = []
    monkeypatch.setattr(auth, "_touch_last_used", touched.append)
    auth.reset_rate_limiter()
    for _ in range(get_settings().rate_limit_per_minute + 10):
        rec.sent.clear()
        await mw(_scope(_service_headers()), _receive, rec.send)
        assert rec.status == 200
    assert touched == []
    assert auth._rate_state == {}


# --- guards that read the code --------------------------------------------


def test_guard_secret_compared_with_compare_digest() -> None:
    source = inspect.getsource(auth.is_service_bearer)
    assert "hmac.compare_digest" in source
    assert "==" not in source


def test_guard_service_check_runs_before_key_lookup() -> None:
    source = Path(inspect.getsourcefile(auth.AuthMiddleware)).read_text(encoding="utf-8")
    body = source[source.index("async def __call__") :]
    assert body.index("is_service_bearer(") < body.index("self._lookup(hash_api_key")


def test_guard_secret_never_logged_or_returned() -> None:
    source = Path(inspect.getsourcefile(auth)).read_text(encoding="utf-8")
    for line in source.splitlines():
        if "logger." in line or "print(" in line:
            assert "secret" not in line.lower()
