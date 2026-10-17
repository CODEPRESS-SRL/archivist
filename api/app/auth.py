"""Bearer API-key authentication for the ``/mcp`` path (T1.2).

A pure ASGI middleware guards ``/mcp`` (and sub-paths): it extracts
``Authorization: Bearer arc_...``, SHA-256s the key and looks it up in
``api_keys`` (``revoked = false``). On success it stores the owner in a
contextvar (later used as ``updated_by`` and for rate limiting) and refreshes
``last_used_at`` best-effort (fire-and-forget, never blocks the request). On a
missing/invalid/revoked key it returns 401 with the canonical Appendix C
message. ``/healthz`` is not under ``/mcp`` and stays unauthenticated.

Only the SHA-256 hex of a key is ever stored; the plaintext exists once, at
creation time. ``create_api_key``/``revoke_api_key`` back both the CLI script
and the tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app import db, errors
from app.config import SERVICE_SECRET_MIN_LENGTH, get_settings
from app.log import get_logger
from app.models import ApiKey, Team

logger = get_logger()

API_KEY_PREFIX = "arc_"

# Service-auth mode (Workspace Node only): ``Authorization: Bearer
# <ARCHIVIST_SERVICE_SECRET>`` + ``X-Archivist-Team: <team name>``.
SERVICE_KEY_ID = 0
SERVICE_OWNER_NAME = "workspace"
SERVICE_TEAM_HEADER = b"x-archivist-team"
SERVICE_TEAM_MAX_LENGTH = 64

# Canonical UNAUTHORIZED / RATE_LIMITED errors (Appendix C); code + text live in
# ``app.errors``, the single source (T1.6). Re-exported here for the 401/429
# responses and tests.
UNAUTHORIZED_CODE = errors.UNAUTHORIZED
UNAUTHORIZED_MESSAGE = errors.UNAUTHORIZED_MESSAGE
RATE_LIMITED_CODE = errors.RATE_LIMITED
RATE_LIMITED_MESSAGE = errors.RATE_LIMITED_MESSAGE

# --- rate limiting (T2.2) --------------------------------------------------
#
# Fixed-window, in-memory per-key counter: ``key_id -> (window, count)`` where
# ``window`` is the integer minute. This is intentionally process-local: it is
# correct for a SINGLE instance (the v1 deployment). With multiple replicas each
# would keep its own counter and the effective limit would multiply — swap this
# for a shared store (e.g. Redis) before scaling horizontally.
_rate_state: dict[int, tuple[int, int]] = {}


def _now() -> float:
    """Monotonic seconds; indirected so tests can freeze/advance the window."""
    return time.monotonic()


def reset_rate_limiter() -> None:
    """Drop all per-key counters (used by tests for isolation)."""
    _rate_state.clear()


def _rate_limit_exceeded(key_id: int, limit: int) -> bool:
    """Count this request in the current minute window; True if it is over ``limit``."""
    window = int(_now() // 60)
    prev_window, count = _rate_state.get(key_id, (window, 0))
    count = count + 1 if prev_window == window else 1
    _rate_state[key_id] = (window, count)
    return count > limit


# Keeps fire-and-forget last_used_at tasks referenced so they are not
# garbage-collected before they run (asyncio holds only a weak reference).
_background_tasks: set[asyncio.Task] = set()


@dataclass(frozen=True)
class AuthContext:
    """The authenticated key, exposed to downstream handlers via ``current_key``."""

    key_id: int
    owner_name: str
    team_id: int
    team_name: str


current_key: ContextVar[AuthContext | None] = ContextVar("current_key", default=None)


async def lookup_auth_context(session: AsyncSession, key_hash: str) -> AuthContext | None:
    """Resolve an API-key hash to its AuthContext, or None if unknown/revoked.

    Single source for "what is a valid key": both the ASGI middleware and the
    admin-console login call this so they never diverge.
    """
    row = (
        await session.execute(
            select(ApiKey.id, ApiKey.owner_name, ApiKey.team_id, Team.name)
            .join(Team, Team.id == ApiKey.team_id)
            .where(ApiKey.key_hash == key_hash, ApiKey.revoked.is_(False))
        )
    ).first()
    if row is None:
        return None
    return AuthContext(
        key_id=row.id, owner_name=row.owner_name, team_id=row.team_id, team_name=row.name
    )


async def lookup_team_context(session: AsyncSession, team_name: str) -> AuthContext | None:
    """Resolve a team name (exact match) to the service AuthContext, or None.

    Used only by the service-auth mode: the Workspace Node names the team in
    ``X-Archivist-Team``. A team that does not exist is NEVER created here.
    The context carries ``key_id=0`` (no ``api_keys`` row) and
    ``owner_name="workspace"``.
    """
    row = (await session.execute(select(Team.id, Team.name).where(Team.name == team_name))).first()
    if row is None:
        return None
    return AuthContext(
        key_id=SERVICE_KEY_ID,
        owner_name=SERVICE_OWNER_NAME,
        team_id=row.id,
        team_name=row.name,
    )


def generate_api_key() -> str:
    """Return a fresh plaintext key: ``arc_`` + 32 url-safe random bytes."""
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(plaintext: str) -> str:
    """Return the SHA-256 hex of a key — the only form that is ever stored."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


async def create_team(session: AsyncSession, name: str) -> int:
    """Insert a team and return its id. Commits."""
    team = Team(name=name)
    session.add(team)
    await session.commit()
    return team.id


async def create_api_key(session: AsyncSession, owner_name: str, team_id: int) -> str:
    """Insert a new key (hash only) for ``team_id`` and return its plaintext once. Commits."""
    plaintext = generate_api_key()
    session.add(ApiKey(key_hash=hash_api_key(plaintext), owner_name=owner_name, team_id=team_id))
    await session.commit()
    return plaintext


async def revoke_api_key(session: AsyncSession, key_id: int) -> bool:
    """Mark a key revoked; return True if a row was affected. Commits."""
    result = await session.execute(update(ApiKey).where(ApiKey.id == key_id).values(revoked=True))
    await session.commit()
    return (result.rowcount or 0) > 0


def _is_protected(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


def _extract_bearer(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            raw = value.decode("latin-1")
            if raw[:7].lower() == "bearer ":
                token = raw[7:].strip()
                return token or None
            return None
    return None


def service_mode_secret(secret: str | None) -> str | None:
    """Return the service secret if the mode is ON, else None.

    The mode is off when the secret is unset or shorter than
    ``SERVICE_SECRET_MIN_LENGTH`` — a weak/absent secret must never open /mcp.
    """
    if secret is None or len(secret) < SERVICE_SECRET_MIN_LENGTH:
        return None
    return secret


def is_service_bearer(token: str, secret: str | None) -> bool:
    """True iff service mode is on and ``token`` equals the secret (constant time)."""
    active = service_mode_secret(secret)
    if active is None:
        return False
    return hmac.compare_digest(token.encode("utf-8"), active.encode("utf-8"))


def validate_team_header(raw: str | None) -> str | None:
    """Return the team name if the header value is acceptable, else None.

    Rejected: missing/empty, longer than 64 characters, or any control
    character (CR, LF, NUL, ...) — the value is never trimmed or normalised,
    the match against ``teams.name`` is exact.
    """
    if not raw or len(raw) > SERVICE_TEAM_MAX_LENGTH:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    return raw


def _extract_team_header(scope: Scope) -> str | None:
    """The single ``X-Archivist-Team`` value, validated; None if absent, repeated or invalid."""
    values = [v for n, v in scope.get("headers", []) if n == SERVICE_TEAM_HEADER]
    if len(values) != 1:
        return None
    return validate_team_header(values[0].decode("latin-1"))


class AuthMiddleware:
    """ASGI middleware enforcing Bearer auth on the ``/mcp`` path."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        settings = get_settings()
        self._rate_limit = settings.rate_limit_per_minute
        self._service_secret = settings.service_secret

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_protected(scope["path"]):
            await self.app(scope, receive, send)
            return

        token = _extract_bearer(scope)
        if token is None:
            await _unauthorized(scope, receive, send)
            return

        # Service mode (Workspace Node): checked BEFORE the key lookup. A bearer
        # that is not the secret falls through to the ``arc_`` path unchanged.
        # No rate limit (the Node enforces its own) and no last_used_at touch.
        if is_service_bearer(token, self._service_secret):
            team_name = _extract_team_header(scope)
            ctx = await self._lookup_team(team_name) if team_name is not None else None
            if ctx is None:
                await _unauthorized(scope, receive, send)
                return
            reset = current_key.set(ctx)
            try:
                await self.app(scope, receive, send)
            finally:
                current_key.reset(reset)
            return

        ctx = await self._lookup(hash_api_key(token))
        if ctx is None:
            await _unauthorized(scope, receive, send)
            return

        if _rate_limit_exceeded(ctx.key_id, self._rate_limit):
            await _rate_limited(scope, receive, send)
            return

        _touch_last_used(ctx.key_id)
        reset = current_key.set(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            current_key.reset(reset)

    async def _lookup(self, key_hash: str) -> AuthContext | None:
        async with db.SessionFactory() as session:
            return await lookup_auth_context(session, key_hash)

    async def _lookup_team(self, team_name: str) -> AuthContext | None:
        async with db.SessionFactory() as session:
            return await lookup_team_context(session, team_name)


def _touch_last_used(key_id: int) -> None:
    """Refresh ``last_used_at`` off the request path; swallow any failure."""

    async def _run() -> None:
        try:
            async with db.SessionFactory() as session:
                await session.execute(
                    update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=func.now())
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 — best-effort, must never affect the request
            logger.warning("last_used_at update failed for key %s: %s", key_id, exc)

    task = asyncio.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _unauthorized(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        status_code=401,
        content={"error": {"code": UNAUTHORIZED_CODE, "message": UNAUTHORIZED_MESSAGE}},
    )
    await response(scope, receive, send)


async def _rate_limited(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        status_code=429,
        content={"error": {"code": RATE_LIMITED_CODE, "message": RATE_LIMITED_MESSAGE}},
    )
    await response(scope, receive, send)
