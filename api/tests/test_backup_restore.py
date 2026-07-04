"""Integration tests for the T3.2 backup/restore scripts.

The scripts (`scripts/backup.sh`, `scripts/restore.sh`) wrap ``pg_dump`` /
``pg_restore`` behind a ``DB_EXEC`` command prefix (default:
``docker compose exec -T db``). Here we point ``DB_EXEC`` at the testcontainer's
own container via ``docker exec -i <id>``, so the real scripts run end-to-end
against a real Postgres. The retention test is filesystem-only (stubbed
``DB_EXEC``) and needs no database.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from app.auth import create_team
from app.services.memory import create_entry, list_entries

_API_DIR = Path(__file__).resolve().parent.parent
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_BACKUP = _SCRIPTS / "backup.sh"
_RESTORE = _SCRIPTS / "restore.sh"

# Resolve bash to a full path: a bare "bash" makes Windows CreateProcess pick
# System32\bash.exe (WSL) over Git Bash, which fails with no distro installed.
_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or _BASH is None,
    reason="backup/restore scripts require docker + bash",
)


def _run_script(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_BASH, str(script), *args],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )


def test_retention_prunes_old_keeps_recent(tmp_path: Path) -> None:
    """Dumps older than RETENTION_DAYS are pruned; recent ones survive."""
    old = tmp_path / "arc_20200101.dump"
    recent = tmp_path / "arc_20240102.dump"
    old.write_bytes(b"old")
    recent.write_bytes(b"recent")
    now = time.time()
    os.utime(old, (now - 15 * 86400 - 3600, now - 15 * 86400 - 3600))  # 15d+ → pruned
    os.utime(recent, (now - 2 * 86400, now - 2 * 86400))  # 2d → kept

    # Stub DB_EXEC so backup.sh needs no database: it just emits fake dump bytes.
    result = _run_script(
        _BACKUP,
        env={
            "DB_EXEC": "printf fakedump",
            "BACKUP_DIR": str(tmp_path),
            "RETENTION_DAYS": "14",
        },
    )
    assert result.returncode == 0, result.stderr

    assert not old.exists(), "15-day-old dump should be pruned"
    assert recent.exists(), "2-day-old dump should be kept"
    stamp = time.strftime("%Y%m%d", time.gmtime())
    today = tmp_path / f"arc_{stamp}.dump"
    assert today.exists(), "today's dump was written"
    assert today.read_bytes() == b"fakedump"


@pytest.mark.parametrize("premigrate", [False, True], ids=["empty-target", "migrated-target"])
async def test_backup_produces_valid_dump_and_restore_roundtrips(
    postgres_container: PostgresContainer, tmp_path: Path, premigrate: bool
) -> None:
    """dump → fresh DB → memory_list returns the pre-dump data (the DoD cycle).

    Runs against both an empty restore target (the DoD's "fresh DB") and a
    pre-migrated one: prod's `api` container runs ``alembic upgrade head`` on
    boot, so a real disaster-recovery restore lands on a non-empty schema. The
    restore must succeed in both cases (``pg_restore --clean --if-exists``).
    """
    cid = postgres_container.get_wrapped_container().id
    db_exec = f"docker exec -i {cid}"
    user = postgres_container.username
    src_db = postgres_container.dbname
    base_url = postgres_container.get_connection_url()

    # 1. Seed a known entry in the source DB via the real service. Unique per
    #    param: both parametrizations share the session container's source DB.
    keyword = f"backup-roundtrip-{'migrated' if premigrate else 'empty'}"
    src_engine = create_async_engine(base_url)
    async with async_sessionmaker(src_engine, expire_on_commit=False)() as s:
        team_id = await create_team(s, f"backup-{'migrated' if premigrate else 'empty'}")
        await create_entry(s, team_id, "archivist", keyword, "canary content for T3.2", "tester")
    await src_engine.dispose()

    # 2. Back up → a custom-format dump lands on the host.
    backup = _run_script(
        _BACKUP,
        env={
            "DB_EXEC": db_exec,
            "BACKUP_DIR": str(tmp_path),
            "POSTGRES_USER": user,
            "POSTGRES_DB": src_db,
            "RETENTION_DAYS": "14",
        },
    )
    assert backup.returncode == 0, backup.stderr
    dumps = list(tmp_path.glob("arc_*.dump"))
    assert len(dumps) == 1
    dump = dumps[0]

    # DoD: the dump is a valid pg_restore archive.
    with dump.open("rb") as fh:
        listing = subprocess.run(
            ["docker", "exec", "-i", cid, "pg_restore", "--list"],
            stdin=fh,
            capture_output=True,
            text=True,
        )
    assert listing.returncode == 0, listing.stderr
    assert "TABLE DATA" in listing.stdout

    # 3. Restore into a fresh database — optionally pre-migrated (the DR case).
    restore_db = "arc_restore_migrated" if premigrate else "arc_restore_empty"
    restore_url = base_url.rsplit("/", 1)[0] + f"/{restore_db}"
    subprocess.run(
        ["docker", "exec", "-i", cid, "dropdb", "-U", user, "--if-exists", restore_db], check=True
    )
    subprocess.run(["docker", "exec", "-i", cid, "createdb", "-U", user, restore_db], check=True)
    if premigrate:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=_API_DIR,
            env={**os.environ, "DATABASE_URL": restore_url},
            check=True,
        )
    restore = _run_script(
        _RESTORE,
        str(dump),
        env={
            "DB_EXEC": db_exec,
            "POSTGRES_USER": user,
            "POSTGRES_DB": restore_db,
            "FORCE": "1",
        },
    )
    assert restore.returncode == 0, restore.stderr

    # 4. DoD: memory_list on the restored DB returns the pre-dump data.
    restore_engine = create_async_engine(restore_url)
    async with async_sessionmaker(restore_engine, expire_on_commit=False)() as s:
        listed = await list_entries(s, team_id, "archivist")
    await restore_engine.dispose()

    keywords = [e["keyword"] for e in listed["entries"]]
    assert keyword in keywords


def test_restore_rejects_missing_dumpfile(tmp_path: Path) -> None:
    """A nonexistent dump path fails fast with a usage exit code, no DB touched."""
    result = _run_script(
        _RESTORE,
        str(tmp_path / "does-not-exist.dump"),
        env={"DB_EXEC": "printf", "POSTGRES_DB": "archivist", "FORCE": "1"},
    )
    assert result.returncode == 2
    assert "not found" in result.stderr
