"""Guard the prod deploy surface declared in ``docker-compose.prod.yml``.

These assertions encode invariants the M3 VPS dry-run verified live:
- every operator-set secret a ``Settings`` field needs actually reaches the
  ``api`` container (regression guard for the ADMIN_SESSION_SECRET /
  FORWARDED_ALLOW_IPS passthrough gap the dry-run caught);
- only Caddy publishes host ports (T3.1 network architecture);
- the db<->api network stays ``internal: true``.

Parsing the compose file directly keeps this a fast unit test (no Docker).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.prod.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def test_api_receives_required_operator_env(compose: dict) -> None:
    """The api container must be handed every secret the operator sets in .env.

    A ``--env-file`` only feeds compose interpolation, not the container env, so
    each variable must be listed explicitly under the api service's environment.
    """
    env = compose["services"]["api"]["environment"]
    assert "DATABASE_URL" in env
    # The two the dry-run found missing: without these the operator's .env value
    # never reaches pydantic Settings inside the container.
    assert "ADMIN_SESSION_SECRET" in env
    assert "FORWARDED_ALLOW_IPS" in env


def test_admin_session_secret_is_mandatory_in_prod(compose: dict) -> None:
    """No stable secret -> ephemeral -> admin sessions drop on every restart."""
    value = compose["services"]["api"]["environment"]["ADMIN_SESSION_SECRET"]
    # ``${VAR:?message}`` makes compose refuse to start when VAR is unset/empty.
    assert value.startswith("${ADMIN_SESSION_SECRET:?")


def test_forwarded_allow_ips_defaults_safe_never_wildcard(compose: dict) -> None:
    value = compose["services"]["api"]["environment"]["FORWARDED_ALLOW_IPS"]
    assert "*" not in value
    # Falls back to the private/loopback ranges when the operator sets nothing.
    assert value.startswith("${FORWARDED_ALLOW_IPS:-")
    assert "127.0.0.1" in value


def test_only_caddy_publishes_host_ports(compose: dict) -> None:
    services = compose["services"]
    assert "ports" in services["caddy"]
    assert "ports" not in services["api"]
    assert "ports" not in services["db"]


def test_internal_network_has_no_internet_route(compose: dict) -> None:
    assert compose["networks"]["internal"]["internal"] is True
    # db lives only on the internal network (no edge, no host).
    assert compose["services"]["db"]["networks"] == ["internal"]
