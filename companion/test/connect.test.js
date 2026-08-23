import { test } from "node:test";
import assert from "node:assert/strict";
import { parseArgs, verify } from "../lib/connect.js";

test("parseArgs: long flags with space form", () => {
  const a = parseArgs(["--server", "https://x/mcp/", "--key", "arc_1", "--scope", "project"]);
  assert.equal(a.server, "https://x/mcp/");
  assert.equal(a.key, "arc_1");
  assert.equal(a.scope, "project");
  assert.equal(a.companion, true);
  assert.equal(a.yes, false);
});

test("parseArgs: --flag=value form", () => {
  const a = parseArgs(["--server=https://x/mcp/", "--key=arc_2"]);
  assert.equal(a.server, "https://x/mcp/");
  assert.equal(a.key, "arc_2");
  assert.equal(a.scope, "user"); // default
});

test("parseArgs: --no-companion and --yes and -h", () => {
  const a = parseArgs(["--no-companion", "--yes", "-h"]);
  assert.equal(a.companion, false);
  assert.equal(a.yes, true);
  assert.equal(a.help, true);
});

test("parseArgs: unknown flags are ignored", () => {
  const a = parseArgs(["--bogus", "x", "--key", "arc_3"]);
  assert.equal(a.key, "arc_3");
});

test("verify: success returns ok and team when exposed", async () => {
  const fakeClient = { memoryList: async () => ({ attribution: "codepress", entries: [] }) };
  const r = await verify({
    serverUrl: "https://x/mcp/", apiKey: "arc_ok", cwd: "/repo",
    createClient: () => fakeClient,
    deriveSlug: () => "billing-api",
  });
  assert.equal(r.ok, true);
  assert.equal(r.team, "codepress");
  assert.equal(r.error, null);
});

test("verify: auth failure returns ok=false with message, no throw", async () => {
  const fakeClient = { memoryList: async () => { throw new Error("MCP HTTP 401"); } };
  const r = await verify({
    serverUrl: "https://x/mcp/", apiKey: "arc_bad", cwd: "/repo",
    createClient: () => fakeClient,
    deriveSlug: () => "billing-api",
  });
  assert.equal(r.ok, false);
  assert.match(r.error, /401/);
});

test("verify: success without attribution field returns team=null", async () => {
  const fakeClient = { memoryList: async () => ({ entries: [] }) };
  const r = await verify({
    serverUrl: "https://x/mcp/", apiKey: "arc_ok", cwd: "/repo",
    createClient: () => fakeClient,
    deriveSlug: () => "billing-api",
  });
  assert.equal(r.ok, true);
  assert.equal(r.team, null);
});

import { registerClaudeMcp } from "../lib/connect.js";

test("registerClaudeMcp: removes then adds, idempotent", () => {
  const calls = [];
  const runClaude = (args) => { calls.push(args); return { notFound: false, status: 0, stdout: "", stderr: "" }; };
  const r = registerClaudeMcp({ serverUrl: "https://x/mcp/", apiKey: "arc_1", scope: "user", runClaude });
  assert.equal(r.ok, true);
  // first call removes (ignoring failure), second adds
  assert.deepEqual(calls[0], ["mcp", "remove", "archivist", "-s", "user"]);
  assert.deepEqual(calls[1], [
    "mcp", "add", "--transport", "http", "archivist", "https://x/mcp/",
    "-H", "Authorization: Bearer arc_1", "-s", "user",
  ]);
});

test("registerClaudeMcp: claude not found → skipped, not error", () => {
  const runClaude = () => ({ notFound: true, status: null, stdout: "", stderr: "" });
  const r = registerClaudeMcp({ serverUrl: "https://x/mcp/", apiKey: "arc_1", scope: "user", runClaude });
  assert.equal(r.ok, false);
  assert.equal(r.skipped, true);
});

test("registerClaudeMcp: add failure surfaces error", () => {
  const runClaude = (args) => args[1] === "add"
    ? { notFound: false, status: 1, stdout: "", stderr: "boom" }
    : { notFound: false, status: 0, stdout: "", stderr: "" };
  const r = registerClaudeMcp({ serverUrl: "https://x/mcp/", apiKey: "arc_1", scope: "user", runClaude });
  assert.equal(r.ok, false);
  assert.match(r.error, /boom/);
});

import { writeAgentConfigs } from "../lib/connect.js";

function memFs(seed = {}) {
  const files = { ...seed };
  const dirs = new Set();
  const modes = {};
  return {
    files,
    dirs,
    modes,
    existsSync: (p) => Object.prototype.hasOwnProperty.call(files, p),
    readFileSync: (p) => files[p],
    writeFileSync: (p, data, opts) => { files[p] = data; if (opts?.mode) modes[p] = opts.mode; },
    mkdirSync: (p) => { dirs.add(p); },
    chmodSync: (p, mode) => { modes[p] = mode; },
    rmSync: (p) => { delete files[p]; },
  };
}

test("writeAgentConfigs: no-op when neither file exists", () => {
  const fs = memFs();
  const r = writeAgentConfigs({ serverUrl: "https://x/mcp/", apiKey: "arc_1", home: "/home/u", fs });
  assert.deepEqual(r, { codex: false, cursor: false });
});

test("writeAgentConfigs: appends Codex block when config.toml exists, idempotent", () => {
  const codexPath = "/home/u/.codex/config.toml";
  const fs = memFs({ [codexPath]: "[other]\nx = 1\n" });
  const r1 = writeAgentConfigs({ serverUrl: "https://x/mcp/", apiKey: "arc_1", home: "/home/u", fs });
  assert.equal(r1.codex, true);
  assert.match(fs.files[codexPath], /\[mcp_servers\.archivist\]/);
  assert.match(fs.files[codexPath], /url = "https:\/\/x\/mcp\/"/);
  // second run must NOT duplicate the block
  const r2 = writeAgentConfigs({ serverUrl: "https://x/mcp/", apiKey: "arc_1", home: "/home/u", fs });
  assert.equal(r2.codex, false);
  assert.equal(fs.files[codexPath].match(/\[mcp_servers\.archivist\]/g).length, 1);
});

test("writeAgentConfigs: merges Cursor JSON when mcp.json exists", () => {
  const cursorPath = "/home/u/.cursor/mcp.json";
  const fs = memFs({ [cursorPath]: JSON.stringify({ mcpServers: { foo: { url: "u" } } }) });
  const r = writeAgentConfigs({ serverUrl: "https://x/mcp/", apiKey: "arc_1", home: "/home/u", fs });
  assert.equal(r.cursor, true);
  const parsed = JSON.parse(fs.files[cursorPath]);
  assert.equal(parsed.mcpServers.foo.url, "u"); // preserved
  assert.equal(parsed.mcpServers.archivist.url, "https://x/mcp/");
  assert.equal(parsed.mcpServers.archivist.headers.Authorization, "Bearer arc_1");
});

import { installCompanion } from "../lib/connect.js";

test("installCompanion: adds local marketplace then installs at scope", () => {
  const calls = [];
  const runClaude = (args) => { calls.push(args); return { notFound: false, status: 0, stdout: "", stderr: "" }; };
  const r = installCompanion({ scope: "user", runClaude, packageRoot: "/pkg" });
  assert.equal(r.ok, true);
  assert.deepEqual(calls[0], ["plugin", "marketplace", "add", "/pkg"]);
  assert.deepEqual(calls[1], ["plugin", "install", "archivist-companion@archivist-tools", "-s", "user"]);
});

test("installCompanion: marketplace 'already exists' is tolerated, install still runs", () => {
  const calls = [];
  const runClaude = (args) => {
    calls.push(args);
    if (args[1] === "marketplace") return { notFound: false, status: 1, stdout: "", stderr: "already exists" };
    return { notFound: false, status: 0, stdout: "", stderr: "" };
  };
  const r = installCompanion({ scope: "user", runClaude, packageRoot: "/pkg" });
  assert.equal(r.ok, true);
  assert.equal(calls.length, 2);
});

test("installCompanion: install failure surfaces error", () => {
  const runClaude = (args) => args[1] === "install"
    ? { notFound: false, status: 1, stdout: "", stderr: "nope" }
    : { notFound: false, status: 0, stdout: "", stderr: "" };
  const r = installCompanion({ scope: "user", runClaude, packageRoot: "/pkg" });
  assert.equal(r.ok, false);
  assert.match(r.error, /nope/);
});

import { writeCompanionConfig } from "../lib/connect.js";
import { resolvePaths } from "../lib/paths.js";

const HOME = "/home/u";
const ROOT = { ARCHIVIST_HOME: "/root/arc" };
const at = (env) => resolvePaths({ env, home: HOME });

test("writeCompanionConfig: writes into the Archivist root, never the home root", () => {
  const fs = memFs();
  const r = writeCompanionConfig({ serverUrl: "https://x/mcp/", apiKey: "arc_1", env: ROOT, home: HOME, fs });
  const paths = at(ROOT);
  assert.equal(r.path, paths.configFile);
  assert.ok(r.path.startsWith(paths.data));
  assert.notEqual(r.path, paths.legacyConfigFile);
  assert.ok(fs.dirs.has(paths.data), "the root is created if missing");
  const parsed = JSON.parse(fs.files[r.path]);
  assert.equal(parsed.serverUrl, "https://x/mcp/");
  assert.equal(parsed.apiKey, "arc_1");
});

test("writeCompanionConfig: the API key file is owner-only", () => {
  const fs = memFs();
  const r = writeCompanionConfig({ serverUrl: "https://x/mcp/", apiKey: "arc_1", env: ROOT, home: HOME, fs });
  assert.equal(fs.modes[r.path], 0o600);
});

test("writeCompanionConfig: preserves unrelated keys already in the file", () => {
  const paths = at(ROOT);
  const fs = memFs({
    [paths.configFile]: JSON.stringify({ captureModel: "sonnet", serverUrl: "old", apiKey: "old" }),
  });
  const r = writeCompanionConfig({
    serverUrl: "https://new/mcp/", apiKey: "arc_new", env: ROOT, home: HOME, fs,
  });
  const parsed = JSON.parse(fs.files[r.path]);
  assert.equal(parsed.captureModel, "sonnet"); // preserved
  assert.equal(parsed.serverUrl, "https://new/mcp/"); // overwritten
  assert.equal(parsed.apiKey, "arc_new");
  assert.equal(r.migrated, false);
});

test("writeCompanionConfig: migrates the pre-1.1 file and removes it", () => {
  const paths = at(ROOT);
  const fs = memFs({
    [paths.legacyConfigFile]: JSON.stringify({ captureModel: "opus", serverUrl: "old", apiKey: "old" }),
  });
  const r = writeCompanionConfig({
    serverUrl: "https://new/mcp/", apiKey: "arc_new", env: ROOT, home: HOME, fs,
  });
  assert.equal(r.migrated, true);
  assert.equal(fs.files[paths.legacyConfigFile], undefined, "nothing is left in the home root");
  const parsed = JSON.parse(fs.files[paths.configFile]);
  assert.equal(parsed.captureModel, "opus", "settings carried over");
  assert.equal(parsed.serverUrl, "https://new/mcp/");
});

import { run } from "../lib/connect.js";

function spyDeps(over = {}) {
  const calls = [];
  const rec = (name, ret) => (...a) => { calls.push({ name, a }); return ret; };
  return {
    calls,
    deps: {
      verify: rec("verify", Promise.resolve({ ok: true, team: "codepress", error: null })),
      registerClaudeMcp: rec("registerClaudeMcp", { ok: true, skipped: false, error: null }),
      writeAgentConfigs: rec("writeAgentConfigs", { codex: false, cursor: false }),
      writeAgentRules: rec("writeAgentRules", { claude: false, codex: false }),
      installCompanion: rec("installCompanion", { ok: true, error: null }),
      writeCompanionConfig: rec("writeCompanionConfig", { path: "/root/arc/companion.json", migrated: false }),
      log: () => {},
      home: "/home/u",
      cwd: "/repo",
      env: {},
      ...over,
    },
  };
}

test("run: --help prints usage and returns 0 without verifying", async () => {
  const { calls, deps } = spyDeps();
  const code = await run({ argv: ["--help"], deps });
  assert.equal(code, 0);
  assert.equal(calls.find((c) => c.name === "verify"), undefined);
});

test("run: missing required args with --yes returns 1", async () => {
  const { deps } = spyDeps();
  const code = await run({ argv: ["--yes"], deps });
  assert.equal(code, 1);
});

test("run: happy path calls all writers in order, returns 0", async () => {
  const { calls, deps } = spyDeps();
  const code = await run({ argv: ["--server", "https://x/mcp/", "--key", "arc_1"], deps });
  assert.equal(code, 0);
  const order = calls.map((c) => c.name);
  assert.deepEqual(order, [
    "verify", "registerClaudeMcp", "writeAgentConfigs", "writeAgentRules", "installCompanion", "writeCompanionConfig",
  ]);
});

test("run: verify failure returns 1 and calls NO writer", async () => {
  const { calls, deps } = spyDeps();
  const rec = (name, ret) => (...a) => { calls.push({ name, a }); return ret; };
  deps.verify = rec("verify", Promise.resolve({ ok: false, team: null, error: "MCP HTTP 401" }));
  const code = await run({ argv: ["--server", "https://x/mcp/", "--key", "arc_1"], deps });
  assert.equal(code, 1);
  const names = calls.map((c) => c.name);
  assert.deepEqual(names, ["verify"]);
});

test("run: --no-companion skips installCompanion and writeCompanionConfig", async () => {
  const { calls, deps } = spyDeps();
  const code = await run({ argv: ["--server", "https://x/mcp/", "--key", "arc_1", "--no-companion"], deps });
  assert.equal(code, 0);
  const names = calls.map((c) => c.name);
  assert.ok(!names.includes("installCompanion"));
  assert.ok(!names.includes("writeCompanionConfig"));
});

test("run: invalid --scope returns 1 before verifying", async () => {
  const { calls, deps } = spyDeps();
  const code = await run({ argv: ["--server", "https://x/mcp/", "--key", "arc_1", "--scope", "bogus"], deps });
  assert.equal(code, 1);
  assert.equal(calls.find((c) => c.name === "verify"), undefined);
});

test("run: writeAgentConfigs throwing warns and continues (fail-soft)", async () => {
  const { calls, deps } = spyDeps();
  deps.writeAgentConfigs = () => { throw new Error("EACCES: permission denied"); };
  const code = await run({ argv: ["--server", "https://x/mcp/", "--key", "arc_1"], deps });
  assert.equal(code, 0);
  const names = calls.map((c) => c.name);
  assert.ok(names.includes("installCompanion"));
  assert.ok(names.includes("writeCompanionConfig"));
});

test("parseArgs: rules defaults true, --no-rules sets false", () => {
  assert.equal(parseArgs(["--key", "arc_1"]).rules, true);
  assert.equal(parseArgs(["--no-rules"]).rules, false);
});

test("run: happy path calls writeAgentRules after writeAgentConfigs", async () => {
  const { calls, deps } = spyDeps();
  deps.writeAgentRules = (...a) => { calls.push({ name: "writeAgentRules", a }); return { claude: true, codex: false }; };
  const code = await run({ argv: ["--server", "https://x/mcp/", "--key", "arc_1"], deps });
  assert.equal(code, 0);
  const names = calls.map((c) => c.name);
  assert.ok(names.includes("writeAgentRules"));
  assert.ok(names.indexOf("writeAgentRules") > names.indexOf("writeAgentConfigs"));
});

test("run: --no-rules skips writeAgentRules", async () => {
  const { calls, deps } = spyDeps();
  deps.writeAgentRules = (...a) => { calls.push({ name: "writeAgentRules", a }); return { claude: false, codex: false }; };
  const code = await run({ argv: ["--server", "https://x/mcp/", "--key", "arc_1", "--no-rules"], deps });
  assert.equal(code, 0);
  assert.ok(!calls.map((c) => c.name).includes("writeAgentRules"));
});

test("run: writeAgentRules throwing warns and continues (fail-soft)", async () => {
  const { calls, deps } = spyDeps();
  deps.writeAgentRules = () => { throw new Error("EACCES"); };
  const code = await run({ argv: ["--server", "https://x/mcp/", "--key", "arc_1"], deps });
  assert.equal(code, 0);
  assert.ok(calls.map((c) => c.name).includes("installCompanion"));
});
