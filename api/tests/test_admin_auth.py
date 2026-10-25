"""Admin console auth: login with a team key, session gating, logout."""

from __future__ import annotations

import httpx
from httpx import ASGITransport

from app.admin.auth import _LOGIN_LIMIT_PER_MIN
from app.main import create_app


def _client(app: object) -> httpx.AsyncClient:
    # https base_url so the Secure/SameSite=strict session cookie is retained.
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="https://arc.test")


async def test_login_with_valid_key_opens_session(db_engine, seed_team) -> None:
    _, plaintext = await seed_team("acme")
    app = create_app()
    async with _client(app) as client:
        resp = await client.post(
            "/admin/login", data={"api_key": plaintext}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin"
        # session cookie now lets us reach a protected page
        page = await client.get("/admin")
        assert page.status_code == 200
        assert "acme" in page.text


async def test_login_with_bad_key_is_rejected(db_engine, seed_team) -> None:
    await seed_team("acme")
    app = create_app()
    async with _client(app) as client:
        resp = await client.post(
            "/admin/login", data={"api_key": "arc_wrong"}, follow_redirects=False
        )
        assert resp.status_code == 200  # re-render login with a generic error
        assert "Invalid" in resp.text or "invalid" in resp.text


async def test_protected_route_without_session_redirects_to_login(db_engine) -> None:
    app = create_app()
    async with _client(app) as client:
        resp = await client.get("/admin", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/login"


def _extract_csrf(html: str) -> str:
    marker = 'name="csrf" value="'
    i = html.index(marker) + len(marker)
    return html[i : html.index('"', i)]


async def test_logout_clears_session(db_engine, seed_team) -> None:
    _, plaintext = await seed_team("acme")
    app = create_app()
    async with _client(app) as client:
        await client.post("/admin/login", data={"api_key": plaintext})
        token = _extract_csrf((await client.get("/admin")).text)
        resp = await client.post("/admin/logout", data={"csrf": token}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/login"
        after = await client.get("/admin", follow_redirects=False)
        assert after.status_code == 303


async def test_logout_rejects_get_and_missing_csrf(db_engine, seed_team) -> None:
    # Logout is state-changing, so it must be a CSRF-guarded POST: a bare GET
    # (e.g. a forged <img>/link) must not be able to end the session.
    _, plaintext = await seed_team("acme")
    app = create_app()
    async with _client(app) as client:
        await client.post("/admin/login", data={"api_key": plaintext})
        assert (await client.get("/admin/logout", follow_redirects=False)).status_code == 405
        bad = await client.post("/admin/logout", follow_redirects=False)
        assert bad.status_code == 400
        # Session survived both attempts.
        assert (await client.get("/admin", follow_redirects=False)).status_code == 200


async def test_login_throttle_trips_after_limit_failed_attempts(db_engine, seed_team) -> None:
    await seed_team("acme")
    app = create_app()
    async with _client(app) as client:
        for _ in range(_LOGIN_LIMIT_PER_MIN):
            resp = await client.post(
                "/admin/login", data={"api_key": "arc_wrong"}, follow_redirects=False
            )
            assert resp.status_code == 200
        throttled = await client.post(
            "/admin/login", data={"api_key": "arc_wrong"}, follow_redirects=False
        )
        assert throttled.status_code == 429


def _proxied_client(app: object, peer: str = "10.9.9.9") -> httpx.AsyncClient:
    """Client whose ASGI peer is a trusted proxy IP, so ProxyHeadersMiddleware
    engages and rewrites the client to the X-Forwarded-For value."""
    transport = ASGITransport(app=app, client=(peer, 0))
    return httpx.AsyncClient(transport=transport, base_url="https://arc.test")


async def test_login_throttle_keys_on_forwarded_client_ip(db_engine, seed_team) -> None:
    # Behind Caddy every request shares the proxy's peer address; the per-IP
    # throttle must bucket by the forwarded client instead, or it collapses to a
    # single global bucket and one abuser locks everyone out.
    await seed_team("acme")
    app = create_app()
    async with _proxied_client(app) as client:
        hdr_a = {"x-forwarded-for": "203.0.113.1"}
        for _ in range(_LOGIN_LIMIT_PER_MIN):
            resp = await client.post(
                "/admin/login", data={"api_key": "arc_wrong"}, headers=hdr_a, follow_redirects=False
            )
            assert resp.status_code == 200
        throttled = await client.post(
            "/admin/login", data={"api_key": "arc_wrong"}, headers=hdr_a, follow_redirects=False
        )
        assert throttled.status_code == 429
        # A different forwarded client behind the same proxy gets its own bucket.
        other = await client.post(
            "/admin/login",
            data={"api_key": "arc_wrong"},
            headers={"x-forwarded-for": "203.0.113.2"},
            follow_redirects=False,
        )
        assert other.status_code == 200


async def test_spoofed_forwarded_for_left_entry_cannot_evade_throttle(db_engine, seed_team) -> None:
    # Caddy appends the real client to X-Forwarded-For, so the true client is the
    # right-most hop. A client rotating a fake left-most entry must not mint fresh
    # throttle buckets — the trust walk keys on the right-most untrusted address.
    await seed_team("acme")
    app = create_app()
    real = "203.0.113.50"
    async with _proxied_client(app) as client:
        for i in range(_LOGIN_LIMIT_PER_MIN):
            resp = await client.post(
                "/admin/login",
                data={"api_key": "arc_wrong"},
                headers={"x-forwarded-for": f"198.51.100.{i}, {real}"},
                follow_redirects=False,
            )
            assert resp.status_code == 200
        # Same real client, yet another spoofed left entry → still throttled.
        blocked = await client.post(
            "/admin/login",
            data={"api_key": "arc_wrong"},
            headers={"x-forwarded-for": f"198.51.100.254, {real}"},
            follow_redirects=False,
        )
        assert blocked.status_code == 429
        # A genuinely different right-most client, even reusing a seen left entry,
        # gets its own bucket — proving the key is the right-most hop, not the peer.
        fresh = await client.post(
            "/admin/login",
            data={"api_key": "arc_wrong"},
            headers={"x-forwarded-for": "198.51.100.0, 203.0.113.99"},
            follow_redirects=False,
        )
        assert fresh.status_code == 200


async def test_successful_login_resets_the_failure_counter(db_engine, seed_team) -> None:
    _, plaintext = await seed_team("acme")
    app = create_app()
    async with _client(app) as client:
        for _ in range(_LOGIN_LIMIT_PER_MIN - 1):
            resp = await client.post(
                "/admin/login", data={"api_key": "arc_wrong"}, follow_redirects=False
            )
            assert resp.status_code == 200
        ok = await client.post("/admin/login", data={"api_key": plaintext}, follow_redirects=False)
        assert ok.status_code == 303
        # If the successful login had not cleared the counter, this bad
        # attempt would already be at the limit and get throttled.
        again = await client.post(
            "/admin/login", data={"api_key": "arc_wrong"}, follow_redirects=False
        )
        assert again.status_code == 200
