import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { resolvePaths, TEMP_PREFIX } from "./paths.js";

// Temporary directories live under the Archivist root (never %TEMP%), always
// carry the `archivist-` prefix, and always embed the owning PID so a later
// startup can tell an abandoned directory from a live one.
//
// Two layers of cleanup:
//   1. deterministic — `withTempDir` removes its directory in a finally block,
//      and process exit / SIGINT / SIGTERM drain the registry (installHandlers);
//   2. best-effort — SIGKILL kills the handlers too, so whatever survives is
//      swept by gc.js on the next startup (dead PID + old enough).

const registry = new Set();
let handlersInstalled = false;

function pidTag(pid) {
  return `${TEMP_PREFIX}${pid}-`;
}

// `archivist-<pid>-<random>` — the PID is what makes the GC safe under
// concurrent sessions: a live owner's directory is never removed.
// `register: false` opts out of exit-time cleanup — needed when the directory
// outlives this process on purpose (a detached child still reading from it).
// The startup GC is what reclaims those, once the owning PID is gone.
export function createTempDir({ paths = resolvePaths(), pid = process.pid, register = true } = {}) {
  mkdirSync(paths.tmp, { recursive: true });
  const dir = mkdtempSync(join(paths.tmp, pidTag(pid)));
  if (register) {
    registry.add(dir);
    installCleanupHandlers();
  }
  return dir;
}

export function removeTempDir(dir) {
  registry.delete(dir);
  try {
    rmSync(dir, { recursive: true, force: true });
  } catch {
    // Best-effort: a locked file on Windows must never surface as a failure.
  }
}

// Context-manager equivalent: the directory is removed whether `fn` returns,
// throws, or rejects.
export async function withTempDir(fn, opts = {}) {
  const dir = createTempDir(opts);
  try {
    return await fn(dir);
  } finally {
    removeTempDir(dir);
  }
}

export function cleanupAll() {
  for (const dir of [...registry]) removeTempDir(dir);
}

// Idempotent. Signal handlers re-raise the default behaviour (exit code
// 128+signo) after cleaning, so the host still sees a normal signal death.
export function installCleanupHandlers({ proc = process } = {}) {
  if (handlersInstalled) return false;
  handlersInstalled = true;
  proc.on("exit", cleanupAll);
  for (const [sig, code] of [["SIGINT", 130], ["SIGTERM", 143]]) {
    proc.on(sig, () => {
      cleanupAll();
      proc.exit(code);
    });
  }
  return true;
}

// Test seam: forget the installed-handlers latch and the tracked dirs.
export function _reset() {
  registry.clear();
  handlersInstalled = false;
}
