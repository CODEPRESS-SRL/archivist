"""Guard the out-of-the-box dev stack declared in ``docker-compose.yml``.

A newcomer running ``docker compose up -d --build`` must get a migrated
database, containers that read ``.env``, and host ports reachable only from
the local machine. Parsing the compose files directly keeps this a fast unit
test (no Docker).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_DEV_COMPOSE = _ROOT / "docker-compose.yml"
_PROD_COMPOSE = _ROOT / "docker-compose.prod.yml"
_ENV_EXAMPLE = _ROOT / ".env.example"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dev_compose() -> dict:
    return _load(_DEV_COMPOSE)


def test_dev_api_migrates_before_serving(dev_compose: dict) -> None:
    command = dev_compose["services"]["api"]["command"]
    joined = " ".join(command) if isinstance(command, list) else command
    assert "alembic upgrade head &&" in joined
    assert joined.index("alembic upgrade head &&") < joined.index("uvicorn")


def test_dev_project_name_differs_from_prod(dev_compose: dict) -> None:
    """Dev and prod on one host must not share containers or the pgdata volume."""
    assert dev_compose["name"] == "archivist-dev"
    assert dev_compose["name"] != _load(_PROD_COMPOSE)["name"]


def test_dev_ports_bound_to_localhost(dev_compose: dict) -> None:
    for service in ("db", "api"):
        ports = dev_compose["services"][service]["ports"]
        assert ports
        for port in ports:
            assert str(port).startswith("127.0.0.1:"), f"{service} publishes {port}"


@pytest.mark.parametrize("compose_path", [_DEV_COMPOSE, _PROD_COMPOSE], ids=lambda p: p.name)
@pytest.mark.parametrize("service", ["api", "embed-worker"])
def test_api_and_worker_read_env_file(compose_path: Path, service: str) -> None:
    env_file = _load(compose_path)["services"][service]["env_file"]
    assert {"path": ".env", "required": False} in env_file


def test_env_example_has_no_active_password() -> None:
    lines = _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    assert not [line for line in lines if re.match(r"^(POSTGRES_PASSWORD|DATABASE_URL)=", line)]
