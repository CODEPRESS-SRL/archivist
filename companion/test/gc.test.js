import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { resolvePaths } from "../lib/paths.js";
import { parseOwnerPid, isPidAlive, collectTempGarbage, pruneCache, runStartupGc } from "../lib/gc.js";

const HOUR = 3600000;

function sandbox() {
  const home = mkdtempSync(join(tmpdir(), "arc-gc-"));
  const paths = resolvePaths({ env: { ARCHIVIST_HOME: join(home, "root") }, home });
  mkdirSync(paths.tmp, { recursive: true });
  return paths;
}

// Create a leftover directory `name`, aged `ageHours` in the past.
function leftover(paths, name, ageHours = 24) {
  const dir = join(paths.tmp, name);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "mcp-config.json"), "{}");
  const when = new Date(Date.now() - ageHours * HOUR);
  utimesSync(dir, when, when);
  return dir;
}

const DEAD = 999999; // never alive in these tests
const dead = () => false;

test("parseOwnerPid reads the PID out of our names and rejects foreign ones", () => {
  assert.equal(parseOwnerPid("archivist-1234-ab12cd"), 1234);
  assert.equal(parseOwnerPid("archivist-7-x"), 7);
  assert.equal(parseOwnerPid("something-else-1234"), null);
  assert.equal(parseOwnerPid("archivist-nopid"), null);
});

test("isPidAlive: this process is alive, an absurd PID is not", () => {
  assert.equal(isPidAlive(process.pid), true);
  assert.equal(isPidAlive(DEAD), false);
  assert.equal(isPidAlive(0), false);
  assert.equal(isPidAlive(-1), false);
  // EPERM means "exists but not ours" — must count as alive.
  assert.equal(isPidAlive(42, () => { const e = new Error("nope"); e.code = "EPERM"; throw e; }), true);
});

test("removes only our prefix, only dead PIDs, only past the age threshold", () => {
  const paths = sandbox();
  const stale = leftover(paths, `archivist-${DEAD}-aaaa`, 24);
  const fresh = leftover(paths, `archivist-${DEAD}-bbbb`, 1);
  const live = leftover(paths, `archivist-${process.pid}-cccc`, 24);
  const foreign = leftover(paths, "someone-elses-tmp", 240);

  const r = collectTempGarbage({ paths, maxAgeHours: 12 });

  assert.ok(!existsSync(stale), "dead + old + ours → removed");
  assert.ok(existsSync(fresh), "too recent → kept");
  assert.ok(existsSync(live), "owner still alive → kept");
  assert.ok(existsSync(foreign), "not ours → never touched");
  assert.equal(r.removed, 1);
  assert.equal(r.kept, 3);
});

test("is idempotent and a no-op on a machine with no temp dir yet", () => {
  const paths = sandbox();
  leftover(paths, `archivist-${DEAD}-aaaa`, 24);
  const first = collectTempGarbage({ paths, maxAgeHours: 12, isAlive: dead });
  const second = collectTempGarbage({ paths, maxAgeHours: 12, isAlive: dead });
  assert.equal(first.removed, 1);
  assert.equal(second.removed, 0);

  const empty = resolvePaths({ env: { ARCHIVIST_HOME: join(tmpdir(), "arc-never-created-xyz") }, home: tmpdir() });
  assert.deepEqual(collectTempGarbage({ paths: empty }), { removed: 0, kept: 0 });
});

test("a failing filesystem never propagates out of the GC", () => {
  const paths = sandbox();
  const boom = () => { throw new Error("EIO"); };
  const fs = { readdirSync: () => [`archivist-${DEAD}-aaaa`], statSync: boom, rmSync: boom };
  assert.doesNotThrow(() => collectTempGarbage({ paths, fs, isAlive: dead }));
  assert.doesNotThrow(() => runStartupGc({ paths, fs: { readdirSync: boom } }));
});

test("pruneCache drops entries past the TTL", () => {
  const paths = sandbox();
  mkdirSync(paths.cache, { recursive: true });
  const old = join(paths.cache, "old.bin");
  const recent = join(paths.cache, "recent.bin");
  writeFileSync(old, "x");
  writeFileSync(recent, "x");
  const when = new Date(Date.now() - 48 * HOUR);
  utimesSync(old, when, when);

  pruneCache({ dir: paths.cache, ttlHours: 24 });
  assert.ok(!existsSync(old));
  assert.ok(existsSync(recent));
});

test("pruneCache evicts oldest-first until under the size cap", () => {
  const paths = sandbox();
  mkdirSync(paths.cache, { recursive: true });
  const names = ["a", "b", "c"];
  names.forEach((n, i) => {
    const f = join(paths.cache, n);
    writeFileSync(f, "x".repeat(100));
    const when = new Date(Date.now() - (names.length - i) * HOUR);
    utimesSync(f, when, when);
  });

  const r = pruneCache({ dir: paths.cache, maxBytes: 150, ttlHours: 1000 });
  assert.ok(!existsSync(join(paths.cache, "a")), "oldest evicted first");
  assert.ok(existsSync(join(paths.cache, "c")), "newest survives");
  assert.ok(r.bytes <= 150);
});

test("runStartupGc sweeps temp and cache in one call", () => {
  const paths = sandbox();
  leftover(paths, `archivist-${DEAD}-aaaa`, 24);
  mkdirSync(paths.companionCache, { recursive: true });
  const old = join(paths.companionCache, "old.bin");
  writeFileSync(old, "x");
  const when = new Date(Date.now() - 24 * 400 * HOUR);
  utimesSync(old, when, when);

  const r = runStartupGc({ paths, maxAgeHours: 12, isAlive: dead });
  assert.equal(r.temp.removed, 1);
  assert.equal(r.cache.removed, 1);
});

test("the size cap never reaches outside the companion's own cache subdir", () => {
  // On Windows the cache root is shared with the server-side app's pinned
  // model caches: a 130 MB fastembed model must survive a 64 MB companion cap.
  const paths = sandbox();
  mkdirSync(join(paths.cache, "fastembed"), { recursive: true });
  const model = join(paths.cache, "fastembed", "model.onnx");
  writeFileSync(model, "x".repeat(5000));
  const when = new Date(Date.now() - 24 * 400 * HOUR);
  utimesSync(model, when, when);

  const r = runStartupGc({ paths, maxAgeHours: 12, isAlive: dead });

  assert.ok(existsSync(model), "someone else's cache is never pruned by us");
  assert.equal(r.cache.removed, 0);
  assert.ok(paths.companionCache.startsWith(paths.cache));
});
