import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, basename } from "node:path";
import { PROTOCOL } from "../lib/protocol.js";
import { resolvePaths } from "../lib/paths.js";
import { _reset } from "../lib/tempdir.js";
import { mcpConfigJson, stageMcpConfig, buildArgs, runHeadless } from "../lib/headless.js";

// Every runHeadless call stages a config file, so each test gets its own
// Archivist root: the real one is never written to by the suite.
function sandbox() {
  const home = mkdtempSync(join(tmpdir(), "arc-headless-"));
  return resolvePaths({ env: { ARCHIVIST_HOME: join(home, "root") }, home });
}

const CFG = { serverUrl: "https://x/mcp/", apiKey: "arc_k" };

test("protocol forbids secrets and mentions the slug rule", () => {
  assert.match(PROTOCOL, /never save/i);
  assert.match(PROTOCOL, /secret/i);
  assert.match(PROTOCOL, /token/i);
  assert.match(PROTOCOL, /slug/i);
});

test("mcpConfigJson builds a Bearer streamable-http server config", () => {
  const j = JSON.parse(mcpConfigJson(CFG));
  assert.equal(j.mcpServers.archivist.type, "http");
  assert.equal(j.mcpServers.archivist.url, "https://x/mcp/");
  assert.equal(j.mcpServers.archivist.headers.Authorization, "Bearer arc_k");
});

test("stageMcpConfig writes the config under the Archivist temp root", () => {
  const paths = sandbox();
  const staged = stageMcpConfig(CFG, { paths });
  try {
    assert.ok(staged.value.startsWith(paths.tmp), "must be staged under the root, not %TEMP%");
    assert.ok(basename(staged.dir).startsWith("archivist-"));
    assert.equal(JSON.parse(readFileSync(staged.value, "utf8")).mcpServers.archivist.url, "https://x/mcp/");
  } finally {
    _reset();
  }
});

test("stageMcpConfig falls back to the inline form when the root is unwritable", () => {
  const paths = sandbox();
  const fs = { writeFileSync: () => { throw new Error("EACCES"); } };
  const staged = stageMcpConfig(CFG, { paths, fs });
  assert.equal(staged.dir, null);
  assert.equal(JSON.parse(staged.value).mcpServers.archivist.url, "https://x/mcp/");
  _reset();
});

test("buildArgs includes print, strict mcp, allowed tools, model, max-turns, json output", () => {
  const args = buildArgs({ prompt: "P", mcpConfig: "{}", allowedTools: "mcp__archivist", model: "haiku", maxTurns: 8 });
  assert.ok(args.includes("-p") && args.includes("P"));
  assert.ok(args.includes("--strict-mcp-config"));
  assert.ok(args.includes("--allowedTools") && args.includes("mcp__archivist"));
  assert.ok(args.includes("--model") && args.includes("haiku"));
  assert.ok(args.includes("--max-turns") && args.includes("8"));
  assert.ok(args.includes("--output-format") && args.includes("json"));
});

test("runHeadless detached fires and forgets with ARCHIVIST_HEADLESS=1", async () => {
  const paths = sandbox();
  let seen;
  const fakeSpawn = (cmd, args, opts) => {
    seen = { cmd, args, opts };
    return { unref() {}, on() {} };
  };
  const out = await runHeadless({
    cfg: CFG,
    prompt: "hi", allowedTools: "mcp__archivist", model: "haiku", maxTurns: 8,
    detached: true, spawn: fakeSpawn, paths,
  });
  assert.deepEqual(out, { detached: true });
  assert.equal(seen.cmd, "claude");
  assert.equal(seen.opts.detached, true);
  assert.equal(seen.opts.env.ARCHIVIST_HEADLESS, "1");
  _reset();
});

test("the API key never reaches the child's command line", async () => {
  const paths = sandbox();
  let seen;
  const fakeSpawn = (cmd, args) => { seen = args; return { unref() {}, on() {} }; };
  await runHeadless({
    cfg: CFG,
    prompt: "hi", allowedTools: "mcp__archivist", model: "haiku", maxTurns: 8,
    detached: true, spawn: fakeSpawn, paths,
  });
  assert.ok(!seen.join(" ").includes("arc_k"), "the key must live in the staged file, not in argv");
  const configPath = seen[seen.indexOf("--mcp-config") + 1];
  assert.ok(configPath.startsWith(paths.tmp));
  assert.match(readFileSync(configPath, "utf8"), /arc_k/);
  _reset();
});

test("a detached run leaves its staged dir for the GC, not for the exit handler", async () => {
  const paths = sandbox();
  const fakeSpawn = () => ({ unref() {}, on() {} });
  await runHeadless({
    cfg: CFG, prompt: "hi", allowedTools: "t", model: "haiku", maxTurns: 8,
    detached: true, spawn: fakeSpawn, paths,
  });
  assert.equal(readdirSync(paths.tmp).length, 1, "the child still needs to read it after we exit");
  _reset();
});

test("runHeadless detached registers an error handler so a spawn error can't crash teardown", async () => {
  const paths = sandbox();
  const events = [];
  const fakeSpawn = () => ({
    on(ev) { events.push(ev); },
    unref() {},
  });
  const out = await runHeadless({
    cfg: CFG,
    prompt: "hi", allowedTools: "mcp__archivist", model: "haiku", maxTurns: 8,
    detached: true, spawn: fakeSpawn, paths,
  });
  assert.deepEqual(out, { detached: true });
  assert.ok(events.includes("error"), "an 'error' listener must be registered on the detached child");
  _reset();
});

test("runHeadless non-detached resolves with the child exit code and cleans up", async () => {
  const paths = sandbox();
  const fakeSpawn = () => {
    const handlers = {};
    setTimeout(() => handlers.close && handlers.close(0), 0);
    return { on(ev, fn) { handlers[ev] = fn; } };
  };
  const out = await runHeadless({
    cfg: CFG,
    prompt: "hi", allowedTools: "mcp__archivist", model: "sonnet", maxTurns: 8,
    spawn: fakeSpawn, paths,
  });
  assert.deepEqual(out, { code: 0 });
  assert.deepEqual(readdirSync(paths.tmp), [], "an awaited run removes its staged dir immediately");
  _reset();
});

test("an awaited run cleans up even when the spawn errors", async () => {
  const paths = sandbox();
  const fakeSpawn = () => {
    const handlers = {};
    setTimeout(() => handlers.error && handlers.error(new Error("ENOENT")), 0);
    return { on(ev, fn) { handlers[ev] = fn; } };
  };
  const out = await runHeadless({
    cfg: CFG, prompt: "hi", allowedTools: "t", model: "sonnet", maxTurns: 8,
    spawn: fakeSpawn, paths,
  });
  assert.deepEqual(out, { error: true });
  assert.deepEqual(readdirSync(paths.tmp), []);
  assert.ok(existsSync(paths.tmp), "the temp root itself stays");
  _reset();
});
