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
  const fakeClient = { memoryList: async () => ({ attribution: "acme", entries: [] }) };
  const r = await verify({
    serverUrl: "https://x/mcp/", apiKey: "arc_ok", cwd: "/repo",
    createClient: () => fakeClient,
    deriveSlug: () => "billing-api",
  });
  assert.equal(r.ok, true);
  assert.equal(r.team, "acme");
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

test("registerClaudeMcp: project scope registers the ${ARCHIVIST_TOKEN} reference, never the key", () => {
  const calls = [];
  const runClaude = (args) => { calls.push(args); return { notFound: false, status: 0, stdout: "", stderr: "" }; };
  const r = registerClaudeMcp({ serverUrl: "https://x/mcp/", apiKey: "arc_secret_1", scope: "project", runClaude });
  assert.equal(r.ok, true);
  assert.deepEqual(calls[1], [
    "mcp", "add", "--transport", "http", "archivist", "https://x/mcp/",
    "-H", "Authorization: Bearer ${ARCHIVIST_TOKEN}", "-s", "project",
  ]);
  assert.ok(!calls.flat().some((arg) => arg.includes("arc_secret_1")));
});

test("registerClaudeMcp: local scope keeps the literal key", () => {
  const calls = [];
  const runClaude = (args) => { calls.push(args); return { notFound: false, status: 0, stdout: "", stderr: "" }; };
  registerClaudeMcp({ serverUrl: "https://x/mcp/", apiKey: "arc_1", scope: "local", runClaude });
  assert.ok(calls[1].includes("Authorization: Bearer arc_1"));
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

function memFs(seed = {}, seedDirs = []) {
  const files = { ...seed };
  const dirs = new Set(seedDirs);
  const modes = {};
  return {
    files,
    dirs,
    modes,
    existsSync: (p) => Object.prototype.hasOwnProperty.call(files, p) || dirs.has(p),
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
  assert.deepEqual(r, { codex: false, cursor: false, cursorInvalid: false });
  assert.deepEqual(fs.files, {});
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

test("writeAgentConfigs: an existing Cursor mcp.json is made owner-only on every write", () => {
  const cursorPath = "/home/u/.cursor/mcp.json";
  const fs = memFs({ [cursorPath]: "{}" });
  writeAgentConfigs({ serverUrl: "https://x/mcp/", apiKey: "arc_1", home: "/home/u", fs });
  assert.equal(fs.modes[cursorPath], 0o600);
});

test("writeAgentConfigs: a failing chmod does not stop the Cursor write", () => {
  const cursorPath = "/home/u/.cursor/mcp.json";
  const fs = memFs({ [cursorPath]: "{}" });
  fs.chmodSync = () => { throw new Error("ENOSYS"); };
  const r = writeAgentConfigs({ serverUrl: "https://x/mcp/", apiKey: "arc_1", home: "/home/u", fs });
  assert.equal(r.cursor, true);
});

test("writeAgentConfigs: the Codex url is a quoted TOML basic string", () => {
  const fs = memFs({}, ["/home/u/.codex"]);
  writeAgentConfigs({ serverUrl: "https://x/mcp/?q=a\\b", apiKey: "arc_1", home: "/home/u", fs });
  assert.match(fs.files["/home/u/.codex/config.toml"], /^url = "https:\/\/x\/mcp\/\?q=a\\\\b"$/m);
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
      verify: rec("verify", Promise.resolve({ ok: true, team: "acme", error: null })),
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

import { defaultRunClaude } from "../lib/connect.js";

const ENOENT_RESULT = { error: Object.assign(new Error("spawn claude ENOENT"), { code: "ENOENT" }), status: null };

function fakeSpawn(respond) {
  const calls = [];
  const spawn = (...a) => { calls.push(a); return respond(...a); };
  return { calls, spawn };
}

test("defaultRunClaude retries claude.cmd through the shell on win32 after ENOENT", () => {
  const { calls, spawn } = fakeSpawn((command) => command === "claude"
    ? ENOENT_RESULT
    : { status: 0, stdout: "ok", stderr: "" });
  const r = defaultRunClaude(["mcp", "remove", "archivist", "-s", "user"], { platform: "win32", spawn });
  assert.deepEqual(r, { notFound: false, status: 0, stdout: "ok", stderr: "" });
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0], [
    "claude", ["mcp", "remove", "archivist", "-s", "user"],
    { encoding: "utf8", env: { ...process.env, NoDefaultCurrentDirectoryInExePath: "1" } },
  ]);
  const [commandString, options, extra] = calls[1];
  assert.equal(commandString, 'claude.cmd "mcp" "remove" "archivist" "-s" "user"');
  assert.equal(options.shell, true);
  assert.equal(options.encoding, "utf8");
  assert.equal(extra, undefined);
});

test("defaultRunClaude shell spawn sets NoDefaultCurrentDirectoryInExePath and no cwd", () => {
  const { calls, spawn } = fakeSpawn((command) => command === "claude"
    ? ENOENT_RESULT
    : { status: 0, stdout: "", stderr: "" });
  defaultRunClaude(["mcp", "list"], { platform: "win32", spawn });
  const options = calls[1][1];
  assert.equal(options.env.NoDefaultCurrentDirectoryInExePath, "1");
  assert.deepEqual(options.env, { ...process.env, NoDefaultCurrentDirectoryInExePath: "1" });
  assert.equal("cwd" in options, false);
});

test("defaultRunClaude resolves both spawns from PATH only, then restores this process's env", () => {
  const guard = "NoDefaultCurrentDirectoryInExePath";
  const previous = process.env[guard];
  delete process.env[guard];
  try {
    const seen = [];
    const spawn = (command, ...rest) => {
      const options = rest.at(-1);
      seen.push({ command, parentGuard: process.env[guard], childGuard: options.env[guard], cwd: options.cwd });
      return command === "claude" ? ENOENT_RESULT : { status: 0, stdout: "", stderr: "" };
    };
    defaultRunClaude(["mcp", "list"], { platform: "win32", spawn });
    assert.deepEqual(seen, [
      { command: "claude", parentGuard: "1", childGuard: "1", cwd: undefined },
      { command: 'claude.cmd "mcp" "list"', parentGuard: "1", childGuard: "1", cwd: undefined },
    ]);
    assert.equal(guard in process.env, false);
  } finally {
    if (previous !== undefined) process.env[guard] = previous;
  }
});

test("defaultRunClaude passes the project token reference through the cmd.exe fallback", () => {
  const { calls, spawn } = fakeSpawn((command) => command === "claude"
    ? ENOENT_RESULT
    : { status: 0, stdout: "", stderr: "" });
  const r = defaultRunClaude(["-H", "Authorization: Bearer ${ARCHIVIST_TOKEN}"], { platform: "win32", spawn });
  assert.equal(r.unsafe, undefined);
  assert.equal(calls[1][0], 'claude.cmd "-H" "Authorization: Bearer ${ARCHIVIST_TOKEN}"');
});

test("defaultRunClaude doubles trailing backslashes before quoting", () => {
  const { calls, spawn } = fakeSpawn((command) => command === "claude"
    ? ENOENT_RESULT
    : { status: 0, stdout: "", stderr: "" });
  defaultRunClaude(["plugin", "marketplace", "add", "C:\\dir\\", "C:\\a\\b", "x\\\\"], { platform: "win32", spawn });
  assert.equal(calls[1][0], 'claude.cmd "plugin" "marketplace" "add" "C:\\dir\\\\" "C:\\a\\b" "x\\\\\\\\"');
});

test("defaultRunClaude refuses shell metacharacters on win32", () => {
  for (const unsafeArg of ["https://x/mcp/?a=1&b=2", "50%", "a|b", "a^b", "a<b", "a>b", 'a"b', "a!b", "a\rb", "a\nb"]) {
    const { calls, spawn } = fakeSpawn(() => ENOENT_RESULT);
    const r = defaultRunClaude(["mcp", "add", unsafeArg], { platform: "win32", spawn });
    assert.equal(r.notFound, false);
    assert.equal(r.unsafe, true);
    assert.equal(calls.length, 1, `no shell spawn for ${JSON.stringify(unsafeArg)}`);
  }
});

test("defaultRunClaude reports notFound when claude.cmd is missing too", () => {
  const notRecognized = [
    "'claude.cmd' is not recognized as an internal or external command,\r\noperable program or batch file.\r\n",
    "\"claude.cmd\" non è riconosciuto come comando interno o esterno,\r\n un programma eseguibile o un file batch.\r\n",
    "Der Befehl \"claude.cmd\" ist entweder falsch geschrieben oder\r\nkonnte nicht gefunden werden.\r\n",
  ];
  for (const stderr of notRecognized) {
    const { calls, spawn } = fakeSpawn((command) => command === "claude"
      ? ENOENT_RESULT
      : { status: 1, stdout: "", stderr });
    const r = defaultRunClaude(["mcp", "list"], { platform: "win32", spawn });
    assert.deepEqual(r, { notFound: true, status: null, stdout: "", stderr: "" });
    assert.equal(calls.length, 2);
  }
});

test("defaultRunClaude surfaces a real claude.cmd failure", () => {
  const { spawn } = fakeSpawn((command) => command === "claude"
    ? ENOENT_RESULT
    : { status: 1, stdout: "", stderr: "Error: unknown scope" });
  const r = defaultRunClaude(["mcp", "list"], { platform: "win32", spawn });
  assert.deepEqual(r, { notFound: false, status: 1, stdout: "", stderr: "Error: unknown scope" });
});

test("defaultRunClaude never uses a shell off win32", () => {
  const { calls, spawn } = fakeSpawn(() => ENOENT_RESULT);
  const r = defaultRunClaude(["mcp", "add", "https://x/mcp/?a=1&b=2"], { platform: "linux", spawn });
  assert.deepEqual(r, { notFound: true, status: null, stdout: "", stderr: "" });
  assert.equal(calls.length, 1);
});

test("defaultRunClaude uses the direct spawn result when claude resolves", () => {
  const { calls, spawn } = fakeSpawn(() => ({ status: 0, stdout: "listed", stderr: "" }));
  const r = defaultRunClaude(["mcp", "list"], { platform: "win32", spawn });
  assert.deepEqual(r, { notFound: false, status: 0, stdout: "listed", stderr: "" });
  assert.equal(calls.length, 1);
});

const UNSAFE_REGISTER_ERROR =
  "server URL or key contains characters that cannot be passed safely to claude.cmd; register manually: " +
  'claude mcp add --transport http archivist <url> -H "Authorization: Bearer <key>" -s user';

test("registerClaudeMcp: unsafe args are a failure with manual instructions", () => {
  const runClaude = (args) => args[1] === "add"
    ? { notFound: false, unsafe: true, status: null, stdout: "", stderr: "" }
    : { notFound: false, status: 0, stdout: "", stderr: "" };
  const r = registerClaudeMcp({ serverUrl: "https://x/mcp/?a=1&b=2", apiKey: "arc_1", scope: "user", runClaude });
  assert.deepEqual(r, { ok: false, skipped: false, error: UNSAFE_REGISTER_ERROR });
});

test("installCompanion: unsafe package path is a failure, install not attempted", () => {
  const calls = [];
  const runClaude = (args) => {
    calls.push(args);
    return { notFound: false, unsafe: true, status: null, stdout: "", stderr: "" };
  };
  const r = installCompanion({ scope: "user", runClaude, packageRoot: "C:/R&D/pkg" });
  assert.equal(r.ok, false);
  assert.match(r.error, /cannot be passed safely to claude\.cmd/);
  assert.equal(calls.length, 1);
});

test("writeAgentConfigs creates codex config when ~/.codex exists but config.toml does not", () => {
  const fs = memFs({}, ["/home/u/.codex"]);
  const r = writeAgentConfigs({ serverUrl: "https://x/mcp/", apiKey: "arc_1", home: "/home/u", fs });
  assert.equal(r.codex, true);
  assert.equal(
    fs.files["/home/u/.codex/config.toml"],
    '[mcp_servers.archivist]\nurl = "https://x/mcp/"\nbearer_token_env_var = "ARCHIVIST_TOKEN"\n',
  );
  assert.equal(fs.files["/home/u/.cursor/mcp.json"], undefined);
});

test("writeAgentConfigs creates cursor mcp.json when ~/.cursor exists but mcp.json does not", () => {
  const fs = memFs({}, ["/home/u/.cursor"]);
  const r = writeAgentConfigs({ serverUrl: "https://x/mcp/", apiKey: "arc_1", home: "/home/u", fs });
  assert.equal(r.cursor, true);
  assert.deepEqual(JSON.parse(fs.files["/home/u/.cursor/mcp.json"]), {
    mcpServers: { archivist: { url: "https://x/mcp/", headers: { Authorization: "Bearer arc_1" } } },
  });
  assert.equal(fs.files["/home/u/.codex/config.toml"], undefined);
  assert.equal(fs.modes["/home/u/.cursor/mcp.json"], 0o600);
});

test("writeAgentConfigs leaves invalid cursor JSON untouched", () => {
  const cursorPath = "/home/u/.cursor/mcp.json";
  for (const invalid of ["{ not json", "null", "[]"]) {
    const fs = memFs({ [cursorPath]: invalid }, ["/home/u/.cursor"]);
    const r = writeAgentConfigs({ serverUrl: "https://x/mcp/", apiKey: "arc_1", home: "/home/u", fs });
    assert.equal(fs.files[cursorPath], invalid);
    assert.equal(r.cursor, false);
    assert.equal(r.cursorInvalid, true);
  }
});

test("run prints the ARCHIVIST_TOKEN hint", async () => {
  const cases = [
    { platform: "win32", hint: 'setx ARCHIVIST_TOKEN "arc_xxx"' },
    { platform: "linux", hint: "export ARCHIVIST_TOKEN=arc_xxx" },
    { platform: "darwin", hint: "export ARCHIVIST_TOKEN=arc_xxx" },
  ];
  for (const { platform, hint } of cases) {
    const lines = [];
    const { deps } = spyDeps({ platform, log: (m) => lines.push(m) });
    deps.writeAgentConfigs = () => ({ codex: true, cursor: false, cursorInvalid: false });
    const code = await run({ argv: ["--server", "https://x/mcp/", "--key", "arc_secret_1"], deps });
    assert.equal(code, 0);
    const output = lines.join("\n");
    assert.ok(output.includes(hint), `${platform} hint missing in:\n${output}`);
    assert.ok(!output.includes("arc_secret_1"), "the real key is never printed");
  }
});

test("run --scope project tells the user to set ARCHIVIST_TOKEN", async () => {
  const cases = [
    { platform: "win32", hint: 'setx ARCHIVIST_TOKEN "arc_xxx"' },
    { platform: "linux", hint: "export ARCHIVIST_TOKEN=arc_xxx" },
  ];
  for (const { platform, hint } of cases) {
    const lines = [];
    const { deps } = spyDeps({ platform, log: (m) => lines.push(m) });
    const code = await run({ argv: ["--server", "https://x/mcp/", "--key", "arc_secret_1", "--scope", "project"], deps });
    assert.equal(code, 0);
    const output = lines.join("\n");
    assert.ok(output.includes("${ARCHIVIST_TOKEN}"), output);
    assert.ok(output.includes(`every teammate must set ARCHIVIST_TOKEN before starting Claude Code: ${hint}`), output);
    assert.ok(!output.includes("arc_secret_1"), "the real key is never printed");
  }
});

test("run --scope user prints no ARCHIVIST_TOKEN hint for Claude Code", async () => {
  const lines = [];
  const { deps } = spyDeps({ log: (m) => lines.push(m) });
  await run({ argv: ["--server", "https://x/mcp/", "--key", "arc_1"], deps });
  assert.ok(!lines.join("\n").includes("ARCHIVIST_TOKEN"));
});

test("run refuses an unsafe or non-http --server before verifying", async () => {
  const bad = [
    "ftp://x/mcp/",
    "file:///etc/passwd",
    "not a url",
    "x/mcp/",
    "https://x/mcp/ extra",
    'https://x/mcp/"',
    "https://x/mcp/'",
    "https://x/mcp/\n[evil]",
    "https://x/mcp/\u0000",
    "https://x/mcp/\t",
  ];
  for (const server of bad) {
    const lines = [];
    const { calls, deps } = spyDeps({ log: (m) => lines.push(m) });
    const code = await run({ argv: ["--server", server, "--key", "arc_1"], deps });
    assert.equal(code, 1, `accepted ${JSON.stringify(server)}`);
    assert.equal(calls.length, 0, "nothing is verified or written");
    assert.match(lines.join("\n"), /^error: --server .*e\.g\. https:\/\/archivist\.example\.com\/mcp\/$/m);
  }
});

test("run accepts http and https --server values", async () => {
  for (const server of ["http://localhost:8000/mcp/", "https://archivist.example.com/mcp/"]) {
    const { calls, deps } = spyDeps();
    const code = await run({ argv: ["--server", server, "--key", "arc_1"], deps });
    assert.equal(code, 0);
    assert.equal(calls[0].a[0].serverUrl, server);
  }
});

test("run warns when cursor mcp.json is invalid", async () => {
  const lines = [];
  const { deps } = spyDeps({ log: (m) => lines.push(m) });
  deps.writeAgentConfigs = () => ({ codex: false, cursor: false, cursorInvalid: true });
  const code = await run({ argv: ["--server", "https://x/mcp/", "--key", "arc_1"], deps });
  assert.equal(code, 0);
  assert.ok(lines.some((l) => l.includes("~/.cursor/mcp.json") && l.includes("not valid JSON")));
});
