"""Single filesystem root per platform — everything Archivist writes lives here.

    Windows   %LOCALAPPDATA%\\Archivist                  cache: ...\\Archivist\\Cache
    macOS     ~/Library/Application Support/Archivist    cache: ~/Library/Caches/Archivist
    Linux     $XDG_DATA_HOME/archivist                   cache: $XDG_CACHE_HOME/archivist
              (fallback ~/.local/share/archivist)               (fallback ~/.cache/archivist)

``ARCHIVIST_HOME`` is the only override and relocates the whole tree.

The API server itself is long-lived and stores its state in Postgres, so its
on-disk footprint is just two third-party caches that would otherwise land in
the OS temp directory: ``fastembed`` (the ~130 MB ONNX model) and ``tiktoken``
(the cl100k_base vocabulary). ``configure_cache_env`` pins both under the root
*without* overriding an explicit setting — the Docker image pins its own, and
that wins.

Run ``python -m app.paths`` to print the inventory, ``--purge`` to remove it.
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

TEMP_PREFIX = "archivist-"
_DEFAULT_MAX_AGE_HOURS = 12


@dataclass(frozen=True)
class Paths:
    data: Path
    cache: Path

    @property
    def tmp(self) -> Path:
        return self.data / "tmp"

    @property
    def logs(self) -> Path:
        return self.data / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs / "archivist.log"


def resolve_paths(
    env: dict[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> Paths:
    """Return the platform's Archivist root. Pure: creates nothing."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    platform = sys.platform if platform is None else platform

    override = (env.get("ARCHIVIST_HOME") or "").strip()
    if override:
        data = Path(override).expanduser().resolve()
        return Paths(data=data, cache=data / "cache")

    if platform == "win32":
        local = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        data = local / "Archivist"
        return Paths(data=data, cache=data / "Cache")

    if platform == "darwin":
        return Paths(
            data=home / "Library" / "Application Support" / "Archivist",
            cache=home / "Library" / "Caches" / "Archivist",
        )

    data_home = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    cache_home = Path(env.get("XDG_CACHE_HOME") or home / ".cache")
    return Paths(data=data_home / "archivist", cache=cache_home / "archivist")


# Third-party caches, keyed by the env var that pins them.
_CACHE_ENV = {
    "FASTEMBED_CACHE_PATH": "fastembed",
    "TIKTOKEN_CACHE_DIR": "tiktoken",
}


def configure_cache_env(
    env: dict[str, str] | None = None, paths: Paths | None = None
) -> dict[str, str]:
    """Pin third-party caches under the Archivist root, unless already set.

    Without this, fastembed downloads its model to ``<tempdir>/fastembed_cache``
    and tiktoken its vocabulary to ``<tempdir>/data-gym-cache`` — outside any
    root we declare and never pruned. Must run before either library is first
    used; both modules call it at import time — so it must never raise, or the
    import fails with it. In particular it resolves the root lazily: a
    deployment that pins both variables (the Docker image does) never touches
    ``Path.home()``, which can itself raise when HOME is unset.
    """
    env = os.environ if env is None else env
    missing = [(var, subdir) for var, subdir in _CACHE_ENV.items() if not env.get(var)]
    if not missing:
        return {}  # everything already pinned; nothing to resolve or create

    if paths is None:
        try:
            paths = resolve_paths(env)
        except (RuntimeError, OSError):
            return {}  # no resolvable home: let each library fall back on its own

    applied: dict[str, str] = {}
    for var, subdir in missing:
        target = paths.cache / subdir
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue  # read-only home: let the library fall back on its own
        env[var] = str(target)
        applied[var] = str(target)
    return applied


# --- temporary directories -------------------------------------------------

_registry: set[Path] = set()
_handlers_installed = False


def _cleanup_all() -> None:
    for path in list(_registry):
        remove_temp_dir(path)


def _install_cleanup_handlers() -> bool:
    """Register atexit + SIGINT/SIGTERM cleanup once. Chains to any existing handler.

    Chaining matters inside the API container: uvicorn installs its own SIGTERM
    handler for graceful shutdown, and replacing it would break the drain.
    """
    global _handlers_installed
    if _handlers_installed:
        return False
    _handlers_installed = True
    atexit.register(_cleanup_all)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous = signal.getsignal(sig)

            def handler(signum, frame, _previous=previous):
                _cleanup_all()
                if callable(_previous):
                    _previous(signum, frame)
                elif _previous == signal.SIG_DFL:
                    signal.signal(signum, signal.SIG_DFL)
                    os.kill(os.getpid(), signum)

            signal.signal(sig, handler)
        except (ValueError, OSError):
            # Not the main thread, or the platform lacks the signal: atexit and
            # the startup GC still cover us.
            continue
    return True


def remove_temp_dir(path: Path) -> None:
    _registry.discard(path)
    shutil.rmtree(path, ignore_errors=True)


@contextmanager
def temp_dir(paths: Paths | None = None, register: bool = True) -> Iterator[Path]:
    """A temp directory under the Archivist root, removed even if the body raises.

    Named ``archivist-<pid>-…`` so a later startup can tell an abandoned
    directory (dead PID) from one still in use.
    """
    paths = resolve_paths() if paths is None else paths
    paths.tmp.mkdir(parents=True, exist_ok=True)
    created = Path(tempfile.mkdtemp(prefix=f"{TEMP_PREFIX}{os.getpid()}-", dir=paths.tmp))
    if register:
        _registry.add(created)
        _install_cleanup_handlers()
    try:
        yield created
    finally:
        remove_temp_dir(created)


# --- startup garbage collection --------------------------------------------


def parse_owner_pid(name: str) -> int | None:
    """``archivist-<pid>-<random>`` -> pid; None when the name is not ours."""
    if not name.startswith(TEMP_PREFIX):
        return None
    rest = name[len(TEMP_PREFIX) :]
    head, sep, _ = rest.partition("-")
    if not sep or not head.isdigit():
        return None
    return int(head)


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def collect_garbage(
    paths: Paths | None = None,
    max_age_hours: float = _DEFAULT_MAX_AGE_HOURS,
    now: float | None = None,
) -> dict[str, int]:
    """Remove temp dirs left by dead sessions. Idempotent; never raises.

    Called on every boot, so resolving the root is guarded too: an
    unresolvable home (``Path.home()`` raises ``RuntimeError`` when HOME is
    unset and the user has no passwd entry) must skip the sweep, never stop
    the process from starting.
    """
    now = time.time() if now is None else now
    result = {"removed": 0, "kept": 0}
    if paths is None:
        try:
            paths = resolve_paths()
        except (RuntimeError, OSError):
            return result
    try:
        entries = list(paths.tmp.iterdir())
    except OSError:
        return result
    for entry in entries:
        try:
            pid = parse_owner_pid(entry.name)
            if pid is None or is_pid_alive(pid):
                result["kept"] += 1
                continue
            if (now - entry.stat().st_mtime) < max_age_hours * 3600:
                result["kept"] += 1
                continue
            shutil.rmtree(entry, ignore_errors=True)
            result["removed"] += 1
        except OSError:
            result["kept"] += 1
    return result


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    value = float(n)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TB"


def describe(paths: Paths | None = None, env: dict[str, str] | None = None) -> str:
    """The `archivist paths` report: what is written, where, and how big."""
    env = os.environ if env is None else env
    paths = resolve_paths(env) if paths is None else paths
    lines: list[str] = []
    if env.get("ARCHIVIST_HOME"):
        lines.append(f"ARCHIVIST_HOME={env['ARCHIVIST_HOME']} (root relocated)")
    rows = [
        ("root", paths.data, "everything Archivist persists"),
        ("cache", paths.cache, "fastembed model + tiktoken vocabulary"),
        ("temp", paths.tmp, f"{TEMP_PREFIX}* dirs, garbage-collected at startup"),
        ("logs", paths.logs, "only when ARCHIVIST_LOG_FILE=1; rotated"),
    ]
    for label, path, note in rows:
        size = f"({_human(_dir_size(path))})" if path.exists() else "(absent)"
        lines.append(f"{label:<7}{path} {size}")
        lines.append(f"{'':<8}{note}")
    lines.append("")
    lines.append("Not on this machine: memories live in Postgres (docker volume 'pgdata'),")
    lines.append("backups in $BACKUP_DIR (default /var/backups/archivist, pruned by retention).")
    return "\n".join(lines)


def purge(paths: Paths | None = None) -> list[Path]:
    """Delete the Archivist root and cache. Returns what was actually removed."""
    paths = resolve_paths() if paths is None else paths
    removed: list[Path] = []
    for target in (paths.data, paths.cache):
        if not target.exists():
            continue
        shutil.rmtree(target, ignore_errors=True)
        if not target.exists():
            removed.append(target)
    return removed


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--purge" in argv:
        removed = purge()
        if not removed:
            print("nothing to remove — no Archivist directory on this machine.")
        for path in removed:
            print(f"removed {path}")
        return 0
    if "--gc" in argv:
        print(collect_garbage())
        return 0
    print(describe())
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
