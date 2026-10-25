import * as nodeFs from "node:fs";
import { join } from "node:path";
import { resolvePaths, TEMP_PREFIX } from "./paths.js";

// Startup garbage collection. Removes what previous sessions left behind —
// including sessions that were SIGKILLed, where no exit handler ever ran.
//
// Deliberately conservative: only paths under the Archivist root, only names
// carrying the `archivist-` prefix, only entries older than `maxAgeHours`, and
// only when the owning PID is gone. Every step is wrapped: a GC failure must
// never delay or block startup.

const HOUR_MS = 3600000;
const DEFAULT_MAX_AGE_HOURS = 12;
const DEFAULT_CACHE_MAX_BYTES = 64 * 1024 * 1024;
const DEFAULT_CACHE_TTL_HOURS = 24 * 30;

// `archivist-<pid>-<random>` → pid, or null when the name is not ours.
export function parseOwnerPid(name) {
  if (!name.startsWith(TEMP_PREFIX)) return null;
  const m = /^archivist-(\d+)-/.exec(name);
  return m ? Number(m[1]) : null;
}

// signal 0 probes existence without delivering anything. EPERM means the
// process exists but belongs to another user — alive, hands off.
export function isPidAlive(pid, kill = process.kill.bind(process)) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    kill(pid, 0);
    return true;
  } catch (err) {
    return err && err.code === "EPERM";
  }
}

export function collectTempGarbage({
  paths = resolvePaths(),
  maxAgeHours = DEFAULT_MAX_AGE_HOURS,
  now = Date.now(),
  isAlive = isPidAlive,
  fs = nodeFs,
} = {}) {
  const result = { removed: 0, kept: 0 };
  let names;
  try {
    names = fs.readdirSync(paths.tmp);
  } catch {
    return result; // no temp dir yet — nothing to do
  }
  for (const name of names) {
    const full = join(paths.tmp, name);
    try {
      const pid = parseOwnerPid(name);
      if (pid === null) {
        result.kept++; // not ours: never touch it
        continue;
      }
      if (isAlive(pid)) {
        result.kept++;
        continue;
      }
      const age = now - fs.statSync(full).mtimeMs;
      if (age < maxAgeHours * HOUR_MS) {
        result.kept++;
        continue;
      }
      fs.rmSync(full, { recursive: true, force: true });
      result.removed++;
    } catch {
      result.kept++; // unreadable or locked: leave it for the next run
    }
  }
  return result;
}

// Bounded cache: drop entries past the TTL, then evict oldest-first until the
// total is under the size cap. No directory under the root may grow forever.
export function pruneCache({
  dir,
  maxBytes = DEFAULT_CACHE_MAX_BYTES,
  ttlHours = DEFAULT_CACHE_TTL_HOURS,
  now = Date.now(),
  fs = nodeFs,
} = {}) {
  const result = { removed: 0, bytes: 0 };
  let names;
  try {
    names = fs.readdirSync(dir);
  } catch {
    return result;
  }
  const entries = [];
  for (const name of names) {
    const full = join(dir, name);
    try {
      const st = fs.statSync(full);
      entries.push({ full, mtimeMs: st.mtimeMs, size: st.size });
    } catch {
      // skip unreadable entries
    }
  }
  const drop = (entry) => {
    try {
      fs.rmSync(entry.full, { recursive: true, force: true });
      result.removed++;
    } catch {
      return false;
    }
    return true;
  };

  const survivors = [];
  for (const entry of entries) {
    if (now - entry.mtimeMs >= ttlHours * HOUR_MS) drop(entry);
    else survivors.push(entry);
  }
  let total = survivors.reduce((n, e) => n + e.size, 0);
  survivors.sort((a, b) => a.mtimeMs - b.mtimeMs); // oldest first
  for (const entry of survivors) {
    if (total <= maxBytes) break;
    if (drop(entry)) total -= entry.size;
  }
  result.bytes = total;
  return result;
}

// The one call every entry point makes. Never throws, never blocks startup.
export function runStartupGc(opts = {}) {
  const paths = opts.paths || resolvePaths();
  try {
    const temp = collectTempGarbage({ ...opts, paths });
    // Only our own subdirectory: the cache root is shared with the server-side
    // app's pinned model caches, which the size cap must never evict.
    const cache = pruneCache({ ...opts, dir: paths.companionCache });
    return { temp, cache };
  } catch {
    return { temp: { removed: 0, kept: 0 }, cache: { removed: 0, bytes: 0 } };
  }
}
