import { test } from "node:test";
import assert from "node:assert/strict";
import { writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadConfig, isConfigured } from "../lib/config.js";

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

test("reads config file from home dir", () => {
  const dir = mkdtempSync(join(tmpdir(), "arc-"));
  writeFileSync(join(dir, ".archivist-companion.json"),
    JSON.stringify({ serverUrl: "https://x/mcp/", apiKey: "arc_k", capture: false, sweepGraceDays: 7 }));
  const cfg = loadConfig({}, dir);
  assert.equal(cfg.serverUrl, "https://x/mcp/");
  assert.equal(cfg.apiKey, "arc_k");
  assert.equal(cfg.capture, false);      // file overrides default
  assert.equal(cfg.sweepGraceDays, 7);
  assert.equal(cfg.captureModel, "haiku"); // untouched default survives
  assert.equal(isConfigured(cfg), true);
});

test("env overrides file and defaults; ARCHIVIST_COMPANION_CONFIG redirects path", () => {
  const dir = mkdtempSync(join(tmpdir(), "arc-"));
  const custom = join(dir, "custom.json");
  writeFileSync(custom, JSON.stringify({ serverUrl: "https://file/mcp/", apiKey: "arc_file" }));
  const cfg = loadConfig(
    { ARCHIVIST_COMPANION_CONFIG: custom, ARCHIVIST_SERVER_URL: "https://env/mcp/", ARCHIVIST_API_KEY: "arc_env" },
    dir
  );
  assert.equal(cfg.serverUrl, "https://env/mcp/"); // env wins
  assert.equal(cfg.apiKey, "arc_env");
});

test("malformed config file falls back to defaults (fail-silent)", () => {
  const dir = mkdtempSync(join(tmpdir(), "arc-"));
  writeFileSync(join(dir, ".archivist-companion.json"), "{ not json");
  const cfg = loadConfig({}, dir);
  assert.equal(isConfigured(cfg), false);
  assert.equal(cfg.capture, true);
});
