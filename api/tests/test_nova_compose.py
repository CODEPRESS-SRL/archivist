"""Guard the private stack declared in ``docker-compose.nova.yml``.

On *nova* Archivist is reached ONLY by the Workspace Node, over the fixed-name
network ``archivist-memoria``. The database sits on an ``internal: true`` network
the Node cannot see. Parsing the compose file keeps this a fast unit test (no
Docker), in the idiom of ``test_prod_compose.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.nova.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def test_db_is_only_on_the_internal_network(compose: dict) -> None:
    assert compose["services"]["db"]["networks"] == ["interna"]


def test_api_bridges_internal_and_memoria(compose: dict) -> None:
    assert sorted(compose["services"]["api"]["networks"]) == ["interna", "memoria"]


def test_worker_is_only_on_the_internal_network(compose: dict) -> None:
    assert compose["services"]["embed-worker"]["networks"] == ["interna"]


def test_no_ports_on_db_and_worker(compose: dict) -> None:
    assert "ports" not in compose["services"]["db"]
    assert "ports" not in compose["services"]["embed-worker"]


def test_only_published_port_is_admin_console_on_loopback(compose: dict) -> None:
    assert compose["services"]["api"]["ports"] == ["127.0.0.1:3006:8000"]


def test_no_public_proxy(compose: dict) -> None:
    assert "caddy" not in compose["services"]
    assert set(compose["services"]) == {"api", "db", "embed-worker"}
    assert "caddy_data" not in compose.get("volumes", {})


@pytest.mark.parametrize("name", ["ARCHIVIST_SERVICE_SECRET", "ADMIN_SESSION_SECRET"])
def test_secrets_are_mandatory_and_reach_api(compose: dict, name: str) -> None:
    value = compose["services"]["api"]["environment"][name]
    # ``${VAR:?message}``: compose refuses to start when VAR is unset/empty.
    assert value.startswith("${" + name + ":?")


def test_api_answers_only_to_the_service_secret(compose: dict) -> None:
    # A literal, not a ``${...}`` from .env: on nova /mcp must refuse every
    # ``arc_`` key, whatever the environment file says (ARC-1).
    assert compose["services"]["api"]["environment"]["ARCHIVIST_SERVICE_ONLY"] == "true"


@pytest.mark.parametrize("service", ["embed-worker", "db"])
def test_service_only_flag_is_api_only(compose: dict, service: str) -> None:
    assert "ARCHIVIST_SERVICE_ONLY" not in compose["services"][service]["environment"]


def test_memoria_network_has_fixed_name_and_is_not_internal(compose: dict) -> None:
    memoria = compose["networks"]["memoria"]
    assert memoria["name"] == "archivist-memoria"
    # Publishing 127.0.0.1:3006 needs a routable network on api.
    assert not memoria.get("internal", False)


def test_internal_network_has_no_internet_route(compose: dict) -> None:
    assert compose["networks"]["interna"]["internal"] is True


def test_every_service_restarts_and_rotates_logs(compose: dict) -> None:
    for name, service in compose["services"].items():
        assert service["restart"] == "unless-stopped", name
        logging = service["logging"]
        assert logging["driver"] == "json-file", name
        assert logging["options"] == {"max-size": "10m", "max-file": "3"}, name
