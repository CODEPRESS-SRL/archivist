"""Filesystem footprint: one declared root per platform, and nothing outside it.

Archivist is installed on other people's machines and killed abruptly by MCP
clients, so where it writes — and what it leaves behind — is a contract.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.paths import (
    TEMP_PREFIX,
    Paths,
    collect_garbage,
    configure_cache_env,
    describe,
    is_pid_alive,
    parse_owner_pid,
    purge,
    resolve_paths,
    temp_dir,
)

DEAD_PID = 999999


def _root(tmp_path: Path) -> Paths:
    return resolve_paths({"ARCHIVIST_HOME": str(tmp_path / "root")}, home=tmp_path)


# --- one root per platform -------------------------------------------------


def test_windows_root_is_localappdata_archivist(tmp_path: Path) -> None:
    paths = resolve_paths(
        {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}, home=Path(r"C:\Users\u"), platform="win32"
    )
    assert paths.data == Path(r"C:\Users\u\AppData\Local") / "Archivist"
    assert paths.cache == paths.data / "Cache"


def test_windows_falls_back_to_home_appdata_local() -> None:
    paths = resolve_paths({}, home=Path(r"C:\Users\u"), platform="win32")
    assert paths.data == Path(r"C:\Users\u") / "AppData" / "Local" / "Archivist"


def test_macos_uses_application_support_and_caches() -> None:
    paths = resolve_paths({}, home=Path("/Users/u"), platform="darwin")
    assert paths.data == Path("/Users/u/Library/Application Support/Archivist")
    assert paths.cache == Path("/Users/u/Library/Caches/Archivist")


def test_linux_honours_xdg_with_documented_fallbacks() -> None:
    xdg = resolve_paths(
        {"XDG_DATA_HOME": "/x/data", "XDG_CACHE_HOME": "/x/cache"},
        home=Path("/home/u"),
        platform="linux",
    )
    assert xdg.data == Path("/x/data/archivist")
    assert xdg.cache == Path("/x/cache/archivist")

    fallback = resolve_paths({}, home=Path("/home/u"), platform="linux")
    assert fallback.data == Path("/home/u/.local/share/archivist")
    assert fallback.cache == Path("/home/u/.cache/archivist")


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_archivist_home_is_the_only_override(platform: str, tmp_path: Path) -> None:
    paths = resolve_paths(
        {"ARCHIVIST_HOME": str(tmp_path)}, home=Path("/home/u"), platform=platform
    )
    assert paths.data == tmp_path.resolve()
    assert paths.cache == tmp_path.resolve() / "cache"
    assert paths.tmp == tmp_path.resolve() / "tmp"


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_nothing_lands_in_the_home_root_or_the_working_directory(platform: str) -> None:
    home = Path("/home/u")
    paths = resolve_paths({}, home=home, platform=platform)
    for path in (paths.tmp, paths.logs, paths.log_file):
        assert paths.data in path.parents or path == paths.data
    assert paths.data != home
    assert Path.cwd() not in paths.data.parents


def test_resolve_paths_creates_nothing(tmp_path: Path) -> None:
    paths = _root(tmp_path)
    assert not paths.data.exists()
    assert not paths.cache.exists()


# --- third-party caches stay inside the root -------------------------------


def test_cache_env_pins_fastembed_and_tiktoken_under_the_root(tmp_path: Path) -> None:
    paths = _root(tmp_path)
    env: dict[str, str] = {}
    applied = configure_cache_env(env, paths)

    assert set(applied) == {"FASTEMBED_CACHE_PATH", "TIKTOKEN_CACHE_DIR"}
    for var, value in applied.items():
        assert Path(value).is_relative_to(paths.cache), var
        assert Path(value).is_dir()
        assert env[var] == value


def test_cache_env_never_overrides_an_explicit_setting(tmp_path: Path) -> None:
    """The Docker image pins its own caches; app defaults must not fight it."""
    paths = _root(tmp_path)
    env = {"FASTEMBED_CACHE_PATH": "/app/.fastembed_cache"}
    applied = configure_cache_env(env, paths)

    assert "FASTEMBED_CACHE_PATH" not in applied
    assert env["FASTEMBED_CACHE_PATH"] == "/app/.fastembed_cache"
    assert "TIKTOKEN_CACHE_DIR" in applied


def test_cache_env_is_idempotent(tmp_path: Path) -> None:
    paths = _root(tmp_path)
    env: dict[str, str] = {}
    first = configure_cache_env(env, paths)
    second = configure_cache_env(env, paths)
    assert first != {}
    assert second == {}  # already pinned


def test_token_counter_does_not_write_outside_the_root(tmp_path: Path, monkeypatch) -> None:
    """Regression: tiktoken used to download cl100k_base into the OS temp dir."""
    monkeypatch.setenv("ARCHIVIST_HOME", str(tmp_path / "root"))
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    code = (
        "from app.utils.tokens import count_tokens;"
        "import os;"
        "assert count_tokens('hello world') > 0;"
        "print(os.environ['TIKTOKEN_CACHE_DIR'])"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"tiktoken vocabulary unavailable offline: {proc.stderr[-300:]}")
    cache_dir = Path(proc.stdout.strip())
    assert cache_dir.is_relative_to(tmp_path.resolve())


# --- temporary directories -------------------------------------------------


def test_temp_dir_lives_under_the_root_and_is_removed(tmp_path: Path) -> None:
    paths = _root(tmp_path)
    with temp_dir(paths) as created:
        assert created.parent == paths.tmp
        assert created.name.startswith(f"{TEMP_PREFIX}{os.getpid()}-")
        (created / "scratch").write_text("x")
        seen = created
    assert not seen.exists()


def test_temp_dir_is_removed_even_when_the_body_raises(tmp_path: Path) -> None:
    paths = _root(tmp_path)
    seen: Path | None = None
    with pytest.raises(ValueError):
        with temp_dir(paths) as created:
            seen = created
            raise ValueError("boom")
    assert seen is not None and not seen.exists()


# --- startup garbage collection --------------------------------------------


def test_parse_owner_pid_only_recognises_our_own_names() -> None:
    assert parse_owner_pid("archivist-1234-abc") == 1234
    assert parse_owner_pid("someone-else-1234") is None
    assert parse_owner_pid("archivist-notapid-x") is None


def test_is_pid_alive() -> None:
    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(DEAD_PID) is False
    assert is_pid_alive(0) is False


def _leftover(paths: Paths, name: str, age_hours: float) -> Path:
    path = paths.tmp / name
    path.mkdir(parents=True)
    (path / "file").write_text("x")
    when = time.time() - age_hours * 3600
    os.utime(path, (when, when))
    return path


def test_gc_removes_only_our_old_dirs_owned_by_dead_processes(tmp_path: Path) -> None:
    paths = _root(tmp_path)
    paths.tmp.mkdir(parents=True)
    stale = _leftover(paths, f"{TEMP_PREFIX}{DEAD_PID}-aaa", 24)
    fresh = _leftover(paths, f"{TEMP_PREFIX}{DEAD_PID}-bbb", 1)
    live = _leftover(paths, f"{TEMP_PREFIX}{os.getpid()}-ccc", 24)
    foreign = _leftover(paths, "someone-elses-dir", 240)

    result = collect_garbage(paths, max_age_hours=12)

    assert not stale.exists()
    assert fresh.exists() and live.exists() and foreign.exists()
    assert result == {"removed": 1, "kept": 3}


def test_gc_is_idempotent_and_silent_on_a_fresh_machine(tmp_path: Path) -> None:
    paths = _root(tmp_path)
    assert collect_garbage(paths) == {"removed": 0, "kept": 0}  # no tmp dir at all

    paths.tmp.mkdir(parents=True)
    _leftover(paths, f"{TEMP_PREFIX}{DEAD_PID}-aaa", 24)
    assert collect_garbage(paths, max_age_hours=12)["removed"] == 1
    assert collect_garbage(paths, max_age_hours=12)["removed"] == 0


def test_gc_never_raises_when_the_filesystem_misbehaves(tmp_path: Path, monkeypatch) -> None:
    paths = _root(tmp_path)
    paths.tmp.mkdir(parents=True)
    _leftover(paths, f"{TEMP_PREFIX}{DEAD_PID}-aaa", 24)

    def boom(*_args, **_kwargs):
        raise OSError("EIO")

    monkeypatch.setattr(Path, "stat", boom)
    assert collect_garbage(paths, max_age_hours=12)["removed"] == 0


# --- the explicit commands -------------------------------------------------


def test_describe_lists_every_writable_location(tmp_path: Path) -> None:
    paths = _root(tmp_path)
    env = {"ARCHIVIST_HOME": str(paths.data)}
    report = describe(paths, env)
    for path in (paths.data, paths.cache, paths.tmp, paths.logs):
        assert str(path) in report
    assert "ARCHIVIST_HOME=" in report
    assert "(absent)" in report
    assert "Postgres" in report  # says what is NOT on this disk
    assert not paths.data.exists(), "the report must be read-only"


def test_purge_removes_the_root_and_reports_it(tmp_path: Path) -> None:
    paths = _root(tmp_path)
    paths.tmp.mkdir(parents=True)
    (paths.data / "companion.json").write_text("{}")
    paths.cache.mkdir(parents=True, exist_ok=True)

    removed = purge(paths)

    assert not paths.data.exists()
    assert paths.data in removed


def test_purge_on_a_clean_machine_is_a_no_op(tmp_path: Path) -> None:
    assert purge(_root(tmp_path)) == []


def test_cache_env_never_resolves_home_when_everything_is_pinned(monkeypatch) -> None:
    """It runs at import time: an unresolvable home must not fail the import."""

    def boom() -> Path:
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(Path, "home", staticmethod(boom))

    pinned = {
        "FASTEMBED_CACHE_PATH": "/app/.fastembed_cache",
        "TIKTOKEN_CACHE_DIR": "/app/.tiktoken_cache",
    }
    assert configure_cache_env(pinned) == {}  # short-circuits before resolving

    # And with nothing pinned it degrades to "let the library decide", not a crash.
    assert configure_cache_env({}) == {}


def test_gc_never_stops_the_process_from_booting(monkeypatch) -> None:
    """create_app() calls it on every boot: an unresolvable home must not raise."""

    def boom() -> Path:
        raise RuntimeError("Could not determine home directory")

    monkeypatch.delenv("ARCHIVIST_HOME", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(boom))
    assert collect_garbage() == {"removed": 0, "kept": 0}
