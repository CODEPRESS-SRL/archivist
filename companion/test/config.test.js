import { test } from "node:test";
import assert from "node:assert/strict";
import { writeFileSync, mkdirSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { loadConfig, isConfigured } from "../lib/config.js";
import { resolvePaths } from "../lib/paths.js";

// Write a config file at the platform's real location for this fake home.
function seedConfig(home, env, data) {
  const { configFile } = resolvePaths({ env, home });
  mkdirSync(dirname(configFile), { recursive: true });
  writeFileSync(configFile, typeof data === "string" ? data : JSON.stringify(data));
  return configFile;
}

test("defaults when no config file and no env", () => {
  const dir = mkdtempSync(join(tmpdir(), "arc-"));
  const cfg = loadConfig({}, dir); // empty env, home dir with no config file
  assert.equal(cfg.serverUrl, null);
  assert.equal(cfg.capture, true);
  assert.equal(cfg.index, true);
  assert.equal(cfg.sweepGraceDays, 3);
  assert.equal(cfg.captureModel, "haiku");
  assert.equal(isConfigured(cfg), false);
});

test("reads config file from the Archivist root", () => {
  const dir = mkdtempSync(join(tmpdir(), "arc-"));
  seedConfig(dir, {}, { serverUrl: "https://x/mcp/", apiKey: "arc_k", capture: false, sweepGraceDays: 7 });
  const cfg = loadConfig({}, dir);
  assert.equal(cfg.serverUrl, "https://x/mcp/");
  assert.equal(cfg.apiKey, "arc_k");
  assert.equal(cfg.capture, false);      // file overrides default
  assert.equal(cfg.sweepGraceDays, 7);
  assert.equal(cfg.captureModel, "haiku"); // untouched default survives
  assert.equal(isConfigured(cfg), true);
});

test("falls back to the pre-1.1 ~/.archivist-companion.json so upgrades keep working", () => {
  const dir = mkdtempSync(join(tmpdir(), "arc-"));
  writeFileSync(join(dir, ".archivist-companion.json"),
    JSON.stringify({ serverUrl: "https://legacy/mcp/", apiKey: "arc_legacy" }));
  const cfg = loadConfig({}, dir);
  assert.equal(cfg.serverUrl, "https://legacy/mcp/");
  assert.equal(cfg.apiKey, "arc_legacy");
});

test("the root config wins over the legacy one", () => {
  const dir = mkdtempSync(join(tmpdir(), "arc-"));
  writeFileSync(join(dir, ".archivist-companion.json"),
    JSON.stringify({ serverUrl: "https://legacy/mcp/", apiKey: "arc_legacy" }));
  seedConfig(dir, {}, { serverUrl: "https://current/mcp/", apiKey: "arc_current" });
  const cfg = loadConfig({}, dir);
  assert.equal(cfg.serverUrl, "https://current/mcp/");
});

test("env overrides file and defaults; ARCHIVIST_HOME relocates the root", () => {
  const dir = mkdtempSync(join(tmpdir(), "arc-"));
  const env = { ARCHIVIST_HOME: join(dir, "custom-root") };
  seedConfig(dir, env, { serverUrl: "https://file/mcp/", apiKey: "arc_file", captureModel: "opus" });

  const relocated = loadConfig(env, dir);
  assert.equal(relocated.serverUrl, "https://file/mcp/", "config is read from ARCHIVIST_HOME");
  assert.equal(relocated.captureModel, "opus");

  const cfg = loadConfig({ ...env, ARCHIVIST_SERVER_URL: "https://env/mcp/", ARCHIVIST_API_KEY: "arc_env" }, dir);
  assert.equal(cfg.serverUrl, "https://env/mcp/"); // env wins
  assert.equal(cfg.apiKey, "arc_env");
});

test("malformed config file falls back to defaults (fail-silent)", () => {
  const dir = mkdtempSync(join(tmpdir(), "arc-"));
  seedConfig(dir, {}, "{ not json");
  const cfg = loadConfig({}, dir);
  assert.equal(isConfigured(cfg), false);
  assert.equal(cfg.capture, true);
});
