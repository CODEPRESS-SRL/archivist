import { test } from "node:test";
import assert from "node:assert/strict";
import { join, resolve } from "node:path";
import { resolvePaths, describePaths, TEMP_PREFIX } from "../lib/paths.js";

const j = (...p) => join(...p);

test("windows: everything under %LOCALAPPDATA%\\Archivist, cache in Cache", () => {
  const p = resolvePaths({ env: { LOCALAPPDATA: "C:/Users/u/AppData/Local" }, home: "C:/Users/u", platform: "win32" });
  assert.equal(p.data, j("C:/Users/u/AppData/Local", "Archivist"));
  assert.equal(p.cache, j("C:/Users/u/AppData/Local", "Archivist", "Cache"));
  assert.equal(p.tmp, j(p.data, "tmp"));
  assert.equal(p.configFile, j(p.data, "companion.json"));
});

test("windows: falls back to <home>/AppData/Local when LOCALAPPDATA is unset", () => {
  const p = resolvePaths({ env: {}, home: "C:/Users/u", platform: "win32" });
  assert.equal(p.data, j("C:/Users/u", "AppData", "Local", "Archivist"));
});

test("macos: Application Support for data, ~/Library/Caches for cache", () => {
  const p = resolvePaths({ env: {}, home: "/Users/u", platform: "darwin" });
  assert.equal(p.data, j("/Users/u", "Library", "Application Support", "Archivist"));
  assert.equal(p.cache, j("/Users/u", "Library", "Caches", "Archivist"));
});

test("linux: XDG dirs honoured, with the spec'd fallbacks", () => {
  const xdg = resolvePaths({
    env: { XDG_DATA_HOME: "/x/data", XDG_CACHE_HOME: "/x/cache" },
    home: "/home/u",
    platform: "linux",
  });
  assert.equal(xdg.data, j("/x/data", "archivist"));
  assert.equal(xdg.cache, j("/x/cache", "archivist"));

  const fallback = resolvePaths({ env: {}, home: "/home/u", platform: "linux" });
  assert.equal(fallback.data, j("/home/u", ".local", "share", "archivist"));
  assert.equal(fallback.cache, j("/home/u", ".cache", "archivist"));
});

test("ARCHIVIST_HOME is the single override and relocates the cache too", () => {
  for (const platform of ["win32", "darwin", "linux"]) {
    const p = resolvePaths({ env: { ARCHIVIST_HOME: "/custom/root" }, home: "/home/u", platform });
    assert.equal(p.data, resolve("/custom/root"));
    assert.equal(p.cache, j(resolve("/custom/root"), "cache"));
    assert.equal(p.tmp, j(resolve("/custom/root"), "tmp"));
  }
});

test("no path escapes the root, and nothing lands in the home root or cwd", () => {
  for (const platform of ["win32", "darwin", "linux"]) {
    const p = resolvePaths({ env: {}, home: "/home/u", platform });
    for (const key of ["tmp", "logs", "configFile", "logFile"]) {
      assert.ok(p[key].startsWith(p.data), `${platform}: ${key} must live under the root`);
    }
    // cache is either inside the root (Windows) or in the platform's cache home
    assert.ok(p.cache.length > 0);
    assert.ok(!p.data.startsWith(process.cwd()), `${platform}: root must not be the working directory`);
  }
});

test("legacy config path is reported so migration can find it", () => {
  const p = resolvePaths({ env: {}, home: "/home/u", platform: "linux" });
  assert.equal(p.legacyConfigFile, j("/home/u", ".archivist-companion.json"));
});

test("describePaths covers every writable location", () => {
  const p = resolvePaths({ env: {}, home: "/home/u", platform: "linux" });
  const labels = describePaths(p).map((d) => d.label);
  assert.deepEqual(labels, ["root", "config", "cache", "temp", "logs"]);
  assert.equal(TEMP_PREFIX, "archivist-");
});

test("ARCHIVIST_HOME expands a leading ~ instead of creating a literal '~' dir", () => {
  const home = "/home/u";
  for (const value of ["~/arc", "~"]) {
    const p = resolvePaths({ env: { ARCHIVIST_HOME: value }, home, platform: "linux" });
    assert.ok(!p.data.includes("~"), `${value} must be expanded, got ${p.data}`);
    assert.ok(!p.data.startsWith(process.cwd()), "must not resolve against the working directory");
  }
  const expanded = resolvePaths({ env: { ARCHIVIST_HOME: "~/arc" }, home, platform: "linux" });
  assert.equal(expanded.data, resolve(join(home, "arc")));
});
