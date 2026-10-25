import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathOnlyEnv, resolveFromPathOnly } from "../lib/exe-path.js";

const GUARD = "NoDefaultCurrentDirectoryInExePath";

test("pathOnlyEnv adds the guard without mutating the base env", () => {
  const base = { PATH: "/bin", ARCHIVIST_HEADLESS: "1" };
  assert.deepEqual(pathOnlyEnv(base), { PATH: "/bin", ARCHIVIST_HEADLESS: "1", [GUARD]: "1" });
  assert.equal(GUARD in base, false);
});

test("pathOnlyEnv defaults to process.env", () => {
  assert.deepEqual(pathOnlyEnv(), { ...process.env, [GUARD]: "1" });
});

test("resolveFromPathOnly sets the guard for the call and removes it afterwards", () => {
  const env = {};
  const seen = resolveFromPathOnly(() => env[GUARD], env);
  assert.equal(seen, "1");
  assert.equal(GUARD in env, false);
});

test("resolveFromPathOnly restores a previous value, even when the call throws", () => {
  const env = { [GUARD]: "0" };
  assert.throws(() => resolveFromPathOnly(() => { throw new Error("spawn failed"); }, env), /spawn failed/);
  assert.equal(env[GUARD], "0");
});

test("a spawn never runs an executable planted in the current directory", { skip: process.platform !== "win32" }, () => {
  const plantedDir = mkdtempSync(join(tmpdir(), "arc-exe-path-"));
  const plantedName = `arc-planted-${process.pid}`;
  copyFileSync(join(process.env.SystemRoot, "System32", "hostname.exe"), join(plantedDir, `${plantedName}.exe`));
  const previousCwd = process.cwd();
  const previousGuard = process.env[GUARD];
  delete process.env[GUARD];
  process.chdir(plantedDir);
  try {
    const guarded = resolveFromPathOnly(() => spawnSync(plantedName, [], { encoding: "utf8", env: pathOnlyEnv() }));
    assert.equal(guarded.error && guarded.error.code, "ENOENT");
  } finally {
    process.chdir(previousCwd);
    if (previousGuard === undefined) delete process.env[GUARD];
    else process.env[GUARD] = previousGuard;
    rmSync(plantedDir, { recursive: true, force: true });
  }
});
