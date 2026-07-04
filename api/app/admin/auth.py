"""Session, CSRF, and login throttle for the admin console.

The team API key is the credential: on login we validate it exactly like the
MCP middleware (shared ``lookup_auth_context``) and store the resolved team in a
signed session cookie. Tenancy downstream is driven only by ``session.team_id``.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from fastapi import Request


class NotAuthenticated(Exception):
    """Raised by ``require_session`` when there is no valid admin session."""


@dataclass(frozen=True)
class AdminSession:
    key_id: int
    owner_name: str
    team_id: int
    team_name: str


def require_session(request: Request) -> AdminSession:
    """Return the current admin session or raise ``NotAuthenticated``."""
    data = request.session.get("admin")
    if not data:
        raise NotAuthenticated()
    return AdminSession(**data)


def set_session(
    request: Request, key_id: int, owner_name: str, team_id: int, team_name: str
) -> None:
    request.session["admin"] = {
        "key_id": key_id,
        "owner_name": owner_name,
        "team_id": team_id,
        "team_name": team_name,
    }


def clear_session(request: Request) -> None:
    request.session.pop("admin", None)
    request.session.pop("csrf", None)


def issue_csrf(request: Request) -> str:
    """Return the session CSRF token, minting one on first use."""
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def check_csrf(request: Request, token: str | None) -> bool:
    expected = request.session.get("csrf")
    return bool(expected) and secrets.compare_digest(str(token), str(expected))


# --- login throttle (per client IP, process-local, single-instance) ----------
_LOGIN_LIMIT_PER_MIN = 10
_login_state: dict[str, tuple[int, int]] = {}


def reset_login_throttle() -> None:
    """Test isolation helper."""
    _login_state.clear()


def login_throttled(ip: str) -> bool:
    """True if ``ip`` has already hit the per-minute failed-login limit."""
    window = int(time.monotonic() // 60)
    prev_window, count = _login_state.get(ip, (window, 0))
    if prev_window != window:
        count = 0
    return count >= _LOGIN_LIMIT_PER_MIN


def record_failed_login(ip: str) -> None:
    """Increment the failed-login count for ``ip`` in the current window."""
    window = int(time.monotonic() // 60)
    prev_window, count = _login_state.get(ip, (window, 0))
    count = count + 1 if prev_window == window else 1
    _login_state[ip] = (window, count)


def clear_login_failures(ip: str) -> None:
    """Drop ``ip``'s failure count, e.g. after a successful login."""
    _login_state.pop(ip, None)
