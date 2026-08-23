import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, basename } from "node:path";
import { resolvePaths, TEMP_PREFIX } from "../lib/paths.js";
import {
  createTempDir,
  removeTempDir,
  withTempDir,
  cleanupAll,
  installCleanupHandlers,
  _reset,
} from "../lib/tempdir.js";

// A throwaway Archivist root per test, so nothing is ever written to the real one.
function sandbox() {
  const home = mkdtempSync(join(tmpdir(), "arc-sandbox-"));
  return resolvePaths({ env: { ARCHIVIST_HOME: join(home, "root") }, home });
}

test("createTempDir lives under the root, is prefixed, and carries the owner PID", () => {
  const paths = sandbox();
  const dir = createTempDir({ paths });
  try {
    assert.ok(dir.startsWith(paths.tmp), "temp dirs must live under the Archivist root, not %TEMP%");
    assert.ok(basename(dir).startsWith(TEMP_PREFIX));
    assert.ok(basename(dir).startsWith(`${TEMP_PREFIX}${process.pid}-`));
    assert.ok(existsSync(dir));
  } finally {
    removeTempDir(dir);
    _reset();
  }
});

test("withTempDir removes the directory on success", async () => {
  const paths = sandbox();
  let seen;
  const out = await withTempDir(async (dir) => {
    seen = dir;
    writeFileSync(join(dir, "f.txt"), "x");
    return 42;
  }, { paths });
  assert.equal(out, 42);
  assert.ok(!existsSync(seen), "directory must be gone after the block");
  _reset();
});

test("withTempDir removes the directory even when the body throws", async () => {
  const paths = sandbox();
  let seen;
  await assert.rejects(
    withTempDir(async (dir) => {
      seen = dir;
      throw new Error("boom");
    }, { paths }),
    /boom/
  );
  assert.ok(!existsSync(seen));
  _reset();
});

test("cleanupAll drains every registered directory", () => {
  const paths = sandbox();
  const a = createTempDir({ paths });
  const b = createTempDir({ paths });
  cleanupAll();
  assert.ok(!existsSync(a) && !existsSync(b));
  assert.equal(readdirSync(paths.tmp).length, 0);
  _reset();
});

test("register:false opts out of exit cleanup (a detached child still needs it)", () => {
  const paths = sandbox();
  const kept = createTempDir({ paths, register: false });
  cleanupAll();
  assert.ok(existsSync(kept), "unregistered dirs survive for the startup GC to reclaim");
  removeTempDir(kept);
  _reset();
});

test("installCleanupHandlers wires exit + SIGINT + SIGTERM exactly once", () => {
  _reset();
  const events = [];
  const proc = { on: (ev) => events.push(ev), exit() {} };
  assert.equal(installCleanupHandlers({ proc }), true);
  assert.deepEqual(events, ["exit", "SIGINT", "SIGTERM"]);
  assert.equal(installCleanupHandlers({ proc }), false, "idempotent");
  assert.equal(events.length, 3);
  _reset();
});

test("SIGTERM handler cleans up and then exits with the signal's code", () => {
  _reset();
  const paths = sandbox();
  const handlers = {};
  const exits = [];
  const proc = { on: (ev, fn) => { handlers[ev] = fn; }, exit: (c) => exits.push(c) };
  installCleanupHandlers({ proc });
  const dir = createTempDir({ paths });
  handlers.SIGTERM();
  assert.ok(!existsSync(dir));
  assert.deepEqual(exits, [143]);
  _reset();
});

test("removeTempDir never throws on a path that is already gone", () => {
  assert.doesNotThrow(() => removeTempDir(join(tmpdir(), "arc-does-not-exist-xyz")));
});
