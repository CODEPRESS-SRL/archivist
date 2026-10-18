"""Guard the pre-built image flow for *nova*.

*nova* has no swap and little free disk, so it must never build the image: it
runs one built elsewhere and loaded with an ID check. The files are read as TEXT
(no Docker, no ssh) so the properties that make the flow trustworthy cannot be
dropped silently: a tag that is mandatory, ``pull_policy: never`` (a missing
image is an error, never a silent pull of a different one), and a loader that
compares the image ID before and after the transfer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE = _REPO / "docker-compose.nova.yml"
_BUILD_OVERRIDE = _REPO / "docker-compose.nova.build.yml"
_SCRIPT = _REPO / "scripts" / "carica-immagine-nova.sh"

_COMPOSE_FLAGS = "-p archivist -f docker-compose.nova.yml"
_IMAGE = "archivist-api:${ARCHIVIST_TAG:?"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_lines(path: Path) -> list[str]:
    """Lines that are neither blank nor comments."""
    return [
        line
        for line in _text(path).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _service_block(name: str) -> str:
    """Text of one top-level service of the compose file."""
    text = _text(_COMPOSE)
    match = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z-]*:\n|^[a-z]+:)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"service {name} not found"
    return match.group(1)


def test_files_exist() -> None:
    assert _COMPOSE.is_file()
    assert _SCRIPT.is_file(), "scripts/carica-immagine-nova.sh is missing"


@pytest.mark.parametrize("service", ["api", "embed-worker"])
def test_service_runs_the_prebuilt_image_and_never_pulls(service: str) -> None:
    block = _service_block(service)
    match = re.search(r"^    image: (.+)$", block, re.MULTILINE)
    assert match, f"{service}: no image:"
    image = match.group(1).strip()
    # The tag is mandatory: an unset ARCHIVIST_TAG must stop `compose`, not
    # fall back to `latest` or to a build on the VPS.
    assert image.startswith(_IMAGE), f"{service}: {image!r}"
    assert re.search(r"^    pull_policy: never$", block, re.MULTILINE)


def test_nova_compose_has_no_build_at_all() -> None:
    # With `build:` present and the image missing, `compose up` BUILDS instead of
    # failing even under pull_policy: never. nova must fail with image-not-found.
    code = "\n".join(_code_lines(_COMPOSE))
    assert not re.search(r"^\s*build:", code, re.MULTILINE)
    assert "dockerfile:" not in code


def test_build_override_exists_and_adds_only_the_api_build() -> None:
    assert _BUILD_OVERRIDE.is_file(), "docker-compose.nova.build.yml is missing"
    code = "\n".join(_code_lines(_BUILD_OVERRIDE))
    assert re.search(r"^  api:$", code, re.MULTILINE)
    assert re.search(r"^    build:$", code, re.MULTILINE)
    assert "context: ./api" in code
    assert "dockerfile: Dockerfile" in code
    assert "embed-worker" not in code
    assert "image:" not in code


def _image_of(service: str) -> str:
    match = re.search(r"^    image: (.+)$", _service_block(service), re.MULTILINE)
    assert match, f"{service}: no image:"
    return match.group(1).strip()


def test_api_and_worker_share_one_image_name() -> None:
    assert _image_of("api") == _image_of("embed-worker")


def test_db_image_is_not_touched() -> None:
    block = _service_block("db")
    assert "image: pgvector/pgvector:pg16" in block
    assert "pull_policy" not in block


def test_script_fails_fast() -> None:
    assert re.search(r"^set -euo pipefail$", _text(_SCRIPT), re.MULTILINE)


def test_script_refuses_without_a_target_host_and_has_no_default() -> None:
    text = _text(_SCRIPT)
    # `:?` stops the script when the variable is unset OR empty; no default host.
    assert re.search(r"ARCHIVIST_NOVA_SSH=\"\$\{ARCHIVIST_NOVA_SSH:\?", text)
    assert not re.search(r"ARCHIVIST_NOVA_SSH:-", text)
    assert not re.search(r"^\s*(export\s+)?ARCHIVIST_NOVA_SSH=[^$\"\s]", text, re.MULTILINE)


def test_script_refuses_an_empty_tag() -> None:
    text = _text(_SCRIPT)
    assert re.search(r"TAG=\"\$\{1:\?", text)
    assert "export ARCHIVIST_TAG" in text


def test_script_builds_with_both_compose_files() -> None:
    text = _text(_SCRIPT)
    assert f"docker compose {_COMPOSE_FLAGS} -f docker-compose.nova.build.yml build api" in text


def test_script_validates_the_tag_before_using_it() -> None:
    text = _text(_SCRIPT)
    # The tag ends up in a remote shell string: only a closed alphabet passes.
    assert "^[A-Za-z0-9_.-]+$" in text
    assert text.index("^[A-Za-z0-9_.-]+$") < text.index("docker compose -p")


def test_script_compares_the_architecture_too() -> None:
    text = _text(_SCRIPT)
    assert text.count("{{.Architecture}}") >= 2
    assert re.search(r'\[\[ "\$\{local_arch\}" != "\$\{remote_arch\}" \]\]', text)


def test_script_documents_the_same_image_store_assumption() -> None:
    assert "image store" in _text(_SCRIPT)


def test_script_transfers_with_save_and_load_over_batch_ssh() -> None:
    text = _text(_SCRIPT)
    assert "docker save" in text
    assert "docker load" in text
    assert "BatchMode=yes" in text
    assert re.search(r"docker save .*\|\s*ssh ", text)


def test_script_compares_the_image_id_before_and_after() -> None:
    text = _text(_SCRIPT)
    assert text.count("image inspect --format '{{.Id}}'") >= 2
    assert re.search(r'\[\[ "\$\{local_id\}" != "\$\{remote_id\}" \]\]', text)
    assert "exit 1" in text


@pytest.mark.parametrize("path", [_COMPOSE, _BUILD_OVERRIDE, _SCRIPT], ids=lambda p: p.name)
def test_every_docker_compose_names_project_and_file(path: Path) -> None:
    # Comments included: a copy-pasted comment is how a bare command sneaks in.
    for line in _text(path).splitlines():
        if "docker compose" in line:
            assert _COMPOSE_FLAGS in line, f"{path.name}: {line.strip()!r}"


def test_no_secret_values_in_the_script() -> None:
    for line in _code_lines(_SCRIPT):
        assert not re.search(r"(PASSWORD|SECRET|TOKEN)=\S+", line), line
