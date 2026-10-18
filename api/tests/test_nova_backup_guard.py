"""Guard the scheduled, self-verifying backup of the *nova* database.

The files under ``deploy/nova/`` are read as TEXT (no Docker, no systemd): the
point is that the properties which make the backup trustworthy cannot be
dropped silently. After the Workspace cutover the Archivist database is the only
copy of recent memory writes, so a backup that targets the wrong compose file,
trusts an empty dump, or fails quietly is worse than no backup.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_NOVA = Path(__file__).resolve().parents[2] / "deploy" / "nova"
_SCRIPT = _NOVA / "archivist-backup.sh"
_SERVICE = _NOVA / "archivist-backup.service"
_TIMER = _NOVA / "archivist-backup.timer"
_FAILED = _NOVA / "archivist-backup-failed.service"

_COMPOSE_FLAGS = "-p archivist -f docker-compose.nova.yml"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_lines(path: Path) -> list[str]:
    """Lines that are neither blank nor comments."""
    return [
        line
        for line in _text(path).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_all_files_exist() -> None:
    for path in (_SCRIPT, _SERVICE, _TIMER, _FAILED):
        assert path.is_file(), f"{path.name} is missing"


def test_script_fails_fast() -> None:
    assert re.search(r"^set -euo pipefail$", _text(_SCRIPT), re.MULTILINE)


def test_script_targets_the_nova_stack_not_the_prod_one() -> None:
    text = _text(_SCRIPT)
    assert re.search(r"^export COMPOSE_FILE=\"?docker-compose\.nova\.yml\"?$", text, re.MULTILINE)
    assert f'export DB_EXEC="docker compose {_COMPOSE_FLAGS} exec -T db"' in text
    assert "docker-compose.prod.yml" not in text


def test_script_wraps_the_existing_backup_with_nova_settings() -> None:
    text = _text(_SCRIPT)
    assert "scripts/backup.sh" in text
    assert re.search(r"BACKUP_DIR=.*/var/backups/archivist-nova", text)
    assert re.search(r"RETENTION_DAYS.*14", text)


def test_script_reopens_the_dump_and_rejects_an_empty_one() -> None:
    text = _text(_SCRIPT)
    # pg_restore -l with no file argument reads the dump from stdin (it does not
    # accept "-" as a file name), run inside the db container via DB_EXEC.
    assert "pg_restore -l" in text
    assert re.search(r"pg_restore -l\s*<", text), "dump must be fed on stdin"
    assert "exit 1" in text


def test_script_mirrors_offsite_and_treats_failure_as_failure() -> None:
    text = _text(_SCRIPT)
    assert "rsync" in text
    assert "--delete" in text
    assert "BatchMode=yes" in text
    assert "OFFSITE_DEST" in text
    # The destination is mandatory: no silent default to a host we did not choose.
    assert re.search(r"OFFSITE_DEST=\"\$\{OFFSITE_DEST:\?", text)
    assert "last-success" in text


def test_script_takes_a_lock_and_uses_no_secrets() -> None:
    text = _text(_SCRIPT)
    assert "flock -n" in text
    for needle in ("POSTGRES_PASSWORD=", "ARCHIVIST_SERVICE_SECRET=", "ADMIN_SESSION_SECRET="):
        assert needle not in text


def test_service_is_a_oneshot_that_runs_the_script_and_fails_loudly() -> None:
    text = _text(_SERVICE)
    assert re.search(r"^OnFailure=archivist-backup-failed\.service$", text, re.MULTILINE)
    assert re.search(r"^Type=oneshot$", text, re.MULTILINE)
    assert re.search(r"^ExecStart=.*/deploy/nova/archivist-backup\.sh$", text, re.MULTILINE)
    assert "After=docker.service" in text


def test_failed_unit_records_state_and_logs_at_error_priority() -> None:
    text = _text(_FAILED)
    assert re.search(r"^Type=oneshot$", text, re.MULTILINE)
    assert "last-failure" in text
    assert "-p err" in text


def test_timer_is_daily_at_0440_utc_and_persistent() -> None:
    text = _text(_TIMER)
    assert re.search(r"^OnCalendar=\*-\*-\* 04:40:00 UTC$", text, re.MULTILINE)
    assert re.search(r"^Persistent=true$", text, re.MULTILINE)
    assert re.search(r"^Unit=archivist-backup\.service$", text, re.MULTILINE)
    assert "WantedBy=timers.target" in text


@pytest.mark.parametrize("path", [_SCRIPT, _SERVICE, _TIMER, _FAILED], ids=lambda p: p.name)
def test_every_docker_compose_names_project_and_file(path: Path) -> None:
    # Comments included: a copy-pasted comment is how a bare command sneaks in.
    for line in _text(path).splitlines():
        if "docker compose" in line:
            assert _COMPOSE_FLAGS in line, f"{path.name}: {line.strip()!r}"


@pytest.mark.parametrize("path", [_SCRIPT, _SERVICE, _TIMER, _FAILED], ids=lambda p: p.name)
def test_no_secret_values_in_the_files(path: Path) -> None:
    for line in _code_lines(path):
        assert not re.search(r"(PASSWORD|SECRET|TOKEN)=\S+", line), (path.name, line)
