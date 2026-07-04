import { test } from "node:test";
import assert from "node:assert/strict";
import { PROTOCOL } from "../lib/protocol.js";
import { mcpConfigJson, buildArgs, runHeadless } from "../lib/headless.js";

test("protocol forbids secrets and mentions the slug rule", () => {
  assert.match(PROTOCOL, /never save/i);
  assert.match(PROTOCOL, /secret/i);
  assert.match(PROTOCOL, /token/i);
  assert.match(PROTOCOL, /slug/i);
});

test("mcpConfigJson builds a Bearer streamable-http server config", () => {
  const j = JSON.parse(mcpConfigJson({ serverUrl: "https://x/mcp/", apiKey: "arc_k" }));
  assert.equal(j.mcpServers.archivist.type, "http");
  assert.equal(j.mcpServers.archivist.url, "https://x/mcp/");
  assert.equal(j.mcpServers.archivist.headers.Authorization, "Bearer arc_k");
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
  let seen;
  const fakeSpawn = (cmd, args, opts) => {
    seen = { cmd, args, opts };
    return { unref() {}, on() {} };
  };
  const out = await runHeadless({
    cfg: { serverUrl: "https://x/mcp/", apiKey: "arc_k" },
    prompt: "hi", allowedTools: "mcp__archivist", model: "haiku", maxTurns: 8,
    detached: true, spawn: fakeSpawn,
  });
  assert.deepEqual(out, { detached: true });
  assert.equal(seen.cmd, "claude");
  assert.equal(seen.opts.detached, true);
  assert.equal(seen.opts.env.ARCHIVIST_HEADLESS, "1");
});

test("runHeadless detached registers an error handler so a spawn error can't crash teardown", async () => {
  const events = [];
  const fakeSpawn = () => ({
    on(ev) { events.push(ev); },
    unref() {},
  });
  const out = await runHeadless({
    cfg: { serverUrl: "https://x/mcp/", apiKey: "arc_k" },
    prompt: "hi", allowedTools: "mcp__archivist", model: "haiku", maxTurns: 8,
    detached: true, spawn: fakeSpawn,
  });
  assert.deepEqual(out, { detached: true });
  assert.ok(events.includes("error"), "an 'error' listener must be registered on the detached child");
});

test("runHeadless non-detached resolves with the child exit code", async () => {
  const fakeSpawn = () => {
    const handlers = {};
    setTimeout(() => handlers.close && handlers.close(0), 0);
    return { on(ev, fn) { handlers[ev] = fn; } };
  };
  const out = await runHeadless({
    cfg: { serverUrl: "https://x/mcp/", apiKey: "arc_k" },
    prompt: "hi", allowedTools: "mcp__archivist", model: "sonnet", maxTurns: 8,
    spawn: fakeSpawn,
  });
  assert.deepEqual(out, { code: 0 });
});
