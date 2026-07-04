# archivist-connect One-Command Setup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `npx archivist-connect`, a single client-side command that verifies a team token against a remote Archivist MCP server, registers the MCP in Claude Code (auto-detecting Codex/Cursor), installs+configures the companion plugin, and prints a summary.

**Architecture:** All logic lives in `companion/lib/connect.js` as small, dependency-injected pure-ish functions (fetch, a `runClaude` command runner, filesystem, env, home, cwd are all injectable). A thin executable `companion/scripts/connect.js` parses `process.argv` and calls `run()`. The connect CLI ships *inside* the `archivist-companion` package, so `npx` already has the plugin files on hand — no marketplace registry, no dependency on the (empty) GitHub repo. Reuses the existing `lib/mcp-client.js` (verify), `lib/config.js` (defaults), `lib/slug.js` (project slug).

**Tech Stack:** Node.js ESM (`"type": "module"`), `node:test` + `node:assert/strict`, `node:child_process` `spawnSync`, `node:fs`, `node:path`, `node:os`. No new runtime dependencies.

## Global Constraints

- **No new runtime dependencies.** Standard library only. (Copied from spec "No new runtime dependencies.")
- **Node ESM.** `import`/`export`, `.js` files, package is `"type": "module"`.
- **Verify before any write.** If the MCP handshake fails, exit non-zero and touch **no** config file, plugin, or MCP registration.
- **Optional steps fail soft.** A missing `claude` CLI or a failed companion install warns and continues; it never aborts the whole command.
- **Fully unit-testable.** Every function takes injectable `fetch` / `runClaude` / `fs` / `env` / `home` / `cwd` deps; tests never touch the real network, the real `claude` CLI, or `~`.
- **LF line endings** (repo `.gitattributes` enforces LF on `*.sh`; keep `.js`/`.json` LF too).
- **Commits:** no `Co-Authored-By` trailer, no agent attribution. Work on local `main`, never push.
- **MCP command shape (verified, Claude Code 2.1.198):**
  - `claude mcp add --transport http archivist <url> -H "Authorization: Bearer <key>" -s <scope>`
  - `claude mcp remove archivist -s <scope>`
  - `claude plugin marketplace add <path>`
  - `claude plugin install archivist-companion@archivist-tools -s <scope>`

## File Structure

- **Create:** `companion/lib/connect.js` — all connect logic (parseArgs, verify, registerClaudeMcp, writeAgentConfigs, installCompanion, writeCompanionConfig, run).
- **Create:** `companion/scripts/connect.js` — thin `#!/usr/bin/env node` entrypoint: `run({ argv: process.argv.slice(2) }).then(code => process.exit(code))`.
- **Create:** `companion/test/connect.test.js` — `node --test` unit tests.
- **Modify:** `companion/package.json` — drop `"private": true`, add `"bin"`.
- **Modify:** `companion/.claude-plugin/marketplace.json` — fix `source` `"./companion"` → `"."`.
- **Modify:** `README.md`, `companion/README.md`, `docs/self-hosting.md`, `CLAUDE.md` (docs).
- **Create:** `docs/agent-commands.md` — reference of every command a developer uses on the agent.

---

### Task 1: Package manifest, marketplace fix, and arg parsing

**Files:**
- Modify: `companion/package.json`
- Modify: `companion/.claude-plugin/marketplace.json`
- Create: `companion/lib/connect.js` (start it — `parseArgs` + `USAGE` only)
- Create: `companion/scripts/connect.js`
- Test: `companion/test/connect.test.js` (start it)

**Interfaces:**
- Produces: `parseArgs(argv: string[]) => { server: string|null, key: string|null, scope: string, companion: boolean, yes: boolean, help: boolean }`
- Produces: `USAGE: string`

- [ ] **Step 1: Write the failing test**

Create `companion/test/connect.test.js`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { parseArgs } from "../lib/connect.js";

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd companion && node --test test/connect.test.js`
Expected: FAIL — cannot import `parseArgs` (module/function missing).

- [ ] **Step 3: Write minimal implementation**

Create `companion/lib/connect.js`:

```js
export const USAGE = `archivist-connect — connect this machine's coding agent to an Archivist server.

Usage:
  npx archivist-connect --server <url> --key <arc_...> [options]

Options:
  --server <url>        Archivist MCP endpoint (e.g. https://host/mcp/)
  --key <arc_...>       Team API key
  --scope <s>           Claude Code scope: user | project | local (default: user)
  --no-companion        Register MCP only; skip companion install + config
  --yes                 Non-interactive; fail instead of prompting for missing values
  -h, --help            Show this help
`;

export function parseArgs(argv) {
  const out = { server: null, key: null, scope: "user", companion: true, yes: false, help: false };
  const it = [...argv];
  while (it.length) {
    let a = it.shift();
    let val = null;
    if (a.startsWith("--")) {
      const eq = a.indexOf("=");
      if (eq !== -1) { val = a.slice(eq + 1); a = a.slice(0, eq); }
    }
    const next = () => (val !== null ? val : it.shift());
    switch (a) {
      case "--server": out.server = next(); break;
      case "--key": out.key = next(); break;
      case "--scope": out.scope = next(); break;
      case "--no-companion": out.companion = false; break;
      case "--yes": out.yes = true; break;
      case "-h":
      case "--help": out.help = true; break;
      default: break;
    }
  }
  return out;
}
```

Create `companion/scripts/connect.js`:

```js
#!/usr/bin/env node
import { run } from "../lib/connect.js";

run({ argv: process.argv.slice(2) }).then((code) => process.exit(code || 0));
```

Modify `companion/package.json` — drop `private`, add `bin`:

```json
{
  "name": "archivist-companion",
  "version": "1.0.0",
  "description": "Optional Claude Code companion for the Archivist shared-memory MCP server: auto-capture, sweep, and session index.",
  "type": "module",
  "bin": { "archivist-connect": "./scripts/connect.js" },
  "scripts": {
    "test": "node --test"
  }
}
```

Fix `companion/.claude-plugin/marketplace.json` — `source` to `"."`:

```json
{
  "name": "archivist-tools",
  "owner": { "name": "Archivist" },
  "plugins": [
    {
      "name": "archivist-companion",
      "source": ".",
      "description": "Session-end auto-capture, sweep, and session-start memory index for Archivist."
    }
  ]
}
```

Note: `run` does not exist yet — Task 7 adds it. `scripts/connect.js` importing it is fine because nothing runs it until then; the `parseArgs` tests import only `parseArgs`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd companion && node --test test/connect.test.js`
Expected: PASS (4 parseArgs tests).

- [ ] **Step 5: Commit**

```bash
git add companion/lib/connect.js companion/scripts/connect.js companion/test/connect.test.js companion/package.json companion/.claude-plugin/marketplace.json
git commit -m "feat(companion): connect CLI scaffold — arg parsing, bin, marketplace source fix"
```

---

### Task 2: Verify step (MCP handshake, fail-fast)

**Files:**
- Modify: `companion/lib/connect.js` (add `verify`)
- Test: `companion/test/connect.test.js`

**Interfaces:**
- Consumes: `createClient({ serverUrl, apiKey, fetch }) => { memoryList(project) }` from `lib/mcp-client.js`; `deriveSlug(cwd)` from `lib/slug.js`.
- Produces: `async verify({ serverUrl, apiKey, cwd, createClient, deriveSlug }) => { ok: boolean, team: string|null, error: string|null }`

- [ ] **Step 1: Write the failing test**

Append to `companion/test/connect.test.js`:

```js
import { verify } from "../lib/connect.js";

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd companion && node --test test/connect.test.js`
Expected: FAIL — `verify` is not exported.

- [ ] **Step 3: Write minimal implementation**

Add to `companion/lib/connect.js` (imports at top of file):

```js
import { createClient as realCreateClient } from "./mcp-client.js";
import { deriveSlug as realDeriveSlug } from "./slug.js";
```

```js
// Confirm the server is reachable and the token authenticates, by issuing a
// real memory_list (which forces the auth path). Never throws.
export async function verify({
  serverUrl,
  apiKey,
  cwd,
  createClient = realCreateClient,
  deriveSlug = realDeriveSlug,
}) {
  try {
    const client = createClient({ serverUrl, apiKey });
    const res = await client.memoryList(deriveSlug(cwd));
    const team = (res && (res.attribution || res.team || res.owner_name)) || null;
    return { ok: true, team, error: null };
  } catch (err) {
    return { ok: false, team: null, error: err && err.message ? err.message : String(err) };
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd companion && node --test test/connect.test.js`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add companion/lib/connect.js companion/test/connect.test.js
git commit -m "feat(companion): connect verify step (fail-fast MCP handshake)"
```

---

### Task 3: Register the MCP in Claude Code

**Files:**
- Modify: `companion/lib/connect.js` (add `defaultRunClaude`, `registerClaudeMcp`)
- Test: `companion/test/connect.test.js`

**Interfaces:**
- Produces: `runClaude(args: string[]) => { notFound: boolean, status: number|null, stdout: string, stderr: string }`
- Produces: `registerClaudeMcp({ serverUrl, apiKey, scope, runClaude }) => { ok: boolean, skipped: boolean, error: string|null }`

- [ ] **Step 1: Write the failing test**

Append to `companion/test/connect.test.js`:

```js
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd companion && node --test test/connect.test.js`
Expected: FAIL — `registerClaudeMcp` not exported.

- [ ] **Step 3: Write minimal implementation**

Add to `companion/lib/connect.js` (import at top):

```js
import { spawnSync } from "node:child_process";
```

```js
// Real command runner. Shell-less spawn; resolves `claude.exe`/`claude` on PATH.
// (Windows .cmd-shim caveat: same as the rest of the companion — a pure .cmd
// shim not resolvable by a shell-less spawn reports notFound.)
export function defaultRunClaude(args) {
  const r = spawnSync("claude", args, { encoding: "utf8" });
  if (r.error && r.error.code === "ENOENT") {
    return { notFound: true, status: null, stdout: "", stderr: "" };
  }
  return { notFound: false, status: r.status, stdout: r.stdout || "", stderr: r.stderr || "" };
}

export function registerClaudeMcp({ serverUrl, apiKey, scope, runClaude = defaultRunClaude }) {
  // Idempotent: remove any existing registration first (ignore its result), then add.
  const probe = runClaude(["mcp", "remove", "archivist", "-s", scope]);
  if (probe.notFound) return { ok: false, skipped: true, error: null };

  const add = runClaude([
    "mcp", "add", "--transport", "http", "archivist", serverUrl,
    "-H", `Authorization: Bearer ${apiKey}`, "-s", scope,
  ]);
  if (add.notFound) return { ok: false, skipped: true, error: null };
  if (add.status !== 0) {
    return { ok: false, skipped: false, error: add.stderr || `claude mcp add exited ${add.status}` };
  }
  return { ok: true, skipped: false, error: null };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd companion && node --test test/connect.test.js`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add companion/lib/connect.js companion/test/connect.test.js
git commit -m "feat(companion): register archivist MCP in Claude Code (idempotent)"
```

---

### Task 4: Auto-detect and write Codex / Cursor MCP config

**Files:**
- Modify: `companion/lib/connect.js` (add `writeAgentConfigs`)
- Test: `companion/test/connect.test.js`

**Interfaces:**
- Produces: `writeAgentConfigs({ serverUrl, apiKey, home, fs }) => { codex: boolean, cursor: boolean }`
  - `fs` shape: `{ existsSync(path), readFileSync(path, enc), writeFileSync(path, data) }` (defaults to `node:fs`).
  - `codex`/`cursor` booleans report whether that file was written.

- [ ] **Step 1: Write the failing test**

Append to `companion/test/connect.test.js`:

```js
import { writeAgentConfigs } from "../lib/connect.js";

function memFs(seed = {}) {
  const files = { ...seed };
  return {
    files,
    existsSync: (p) => Object.prototype.hasOwnProperty.call(files, p),
    readFileSync: (p) => files[p],
    writeFileSync: (p, data) => { files[p] = data; },
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd companion && node --test test/connect.test.js`
Expected: FAIL — `writeAgentConfigs` not exported.

- [ ] **Step 3: Write minimal implementation**

Add to `companion/lib/connect.js` (imports at top):

```js
import * as nodeFs from "node:fs";
import { join } from "node:path";
```

```js
// Only touches a client's config if it ALREADY exists (auto-detect). Never
// creates Codex/Cursor config from scratch; never installs the companion for them.
export function writeAgentConfigs({ serverUrl, apiKey, home, fs = nodeFs }) {
  const result = { codex: false, cursor: false };

  const codexPath = join(home, ".codex", "config.toml");
  if (fs.existsSync(codexPath)) {
    const cur = fs.readFileSync(codexPath, "utf8");
    if (!cur.includes("[mcp_servers.archivist]")) {
      const block =
        `\n[mcp_servers.archivist]\n` +
        `url = "${serverUrl}"\n` +
        `bearer_token_env_var = "ARCHIVIST_TOKEN"\n`;
      fs.writeFileSync(codexPath, cur.endsWith("\n") ? cur + block : cur + "\n" + block);
      result.codex = true;
    }
  }

  const cursorPath = join(home, ".cursor", "mcp.json");
  if (fs.existsSync(cursorPath)) {
    let json = {};
    try { json = JSON.parse(fs.readFileSync(cursorPath, "utf8")); } catch { json = {}; }
    if (!json.mcpServers) json.mcpServers = {};
    json.mcpServers.archivist = { url: serverUrl, headers: { Authorization: `Bearer ${apiKey}` } };
    fs.writeFileSync(cursorPath, JSON.stringify(json, null, 2) + "\n");
    result.cursor = true;
  }

  return result;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd companion && node --test test/connect.test.js`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add companion/lib/connect.js companion/test/connect.test.js
git commit -m "feat(companion): auto-detect Codex/Cursor and write archivist MCP block"
```

---

### Task 5: Install the companion plugin (local marketplace)

**Files:**
- Modify: `companion/lib/connect.js` (add `defaultPackageRoot`, `installCompanion`)
- Test: `companion/test/connect.test.js`

**Interfaces:**
- Consumes: `runClaude` from Task 3.
- Produces: `installCompanion({ scope, runClaude, packageRoot }) => { ok: boolean, error: string|null }`
- Produces: `defaultPackageRoot() => string` (the companion package dir, derived from `import.meta.url`).

- [ ] **Step 1: Write the failing test**

Append to `companion/test/connect.test.js`:

```js
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd companion && node --test test/connect.test.js`
Expected: FAIL — `installCompanion` not exported.

- [ ] **Step 3: Write minimal implementation**

Add to `companion/lib/connect.js` (imports at top):

```js
import { fileURLToPath } from "node:url";
import { dirname } from "node:path";
```

```js
// The companion package dir (contains .claude-plugin/marketplace.json).
// lib/connect.js -> lib -> package root.
export function defaultPackageRoot() {
  return dirname(dirname(fileURLToPath(import.meta.url)));
}

// Register the shipped plugin dir as a local marketplace, then install it.
// The marketplace add is best-effort (a pre-existing marketplace is fine); only
// a failed install is a real error. Returns skipped-as-error=false on notFound
// so the orchestrator can warn without aborting.
export function installCompanion({ scope, runClaude = defaultRunClaude, packageRoot = defaultPackageRoot() }) {
  const mkt = runClaude(["plugin", "marketplace", "add", packageRoot]);
  if (mkt.notFound) return { ok: false, error: "claude CLI not found" };
  // status !== 0 is tolerated here (marketplace may already be registered).

  const inst = runClaude(["plugin", "install", "archivist-companion@archivist-tools", "-s", scope]);
  if (inst.notFound) return { ok: false, error: "claude CLI not found" };
  if (inst.status !== 0) {
    return { ok: false, error: inst.stderr || `claude plugin install exited ${inst.status}` };
  }
  return { ok: true, error: null };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd companion && node --test test/connect.test.js`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add companion/lib/connect.js companion/test/connect.test.js
git commit -m "feat(companion): install companion plugin via local marketplace"
```

---

### Task 6: Write the companion config file (merge, preserve keys)

**Files:**
- Modify: `companion/lib/connect.js` (add `writeCompanionConfig`)
- Test: `companion/test/connect.test.js`

**Interfaces:**
- Produces: `writeCompanionConfig({ serverUrl, apiKey, env, home, fs }) => { path: string }`
  - Path resolution matches `lib/config.js`: `env.ARCHIVIST_COMPANION_CONFIG || join(home, ".archivist-companion.json")`.
  - Merges into any existing file, overwriting only `serverUrl`/`apiKey`.

- [ ] **Step 1: Write the failing test**

Append to `companion/test/connect.test.js`:

```js
import { writeCompanionConfig } from "../lib/connect.js";

test("writeCompanionConfig: creates file with serverUrl+apiKey at default path", () => {
  const fs = memFs();
  const r = writeCompanionConfig({ serverUrl: "https://x/mcp/", apiKey: "arc_1", env: {}, home: "/home/u", fs });
  assert.equal(r.path, "/home/u/.archivist-companion.json");
  const parsed = JSON.parse(fs.files[r.path]);
  assert.equal(parsed.serverUrl, "https://x/mcp/");
  assert.equal(parsed.apiKey, "arc_1");
});

test("writeCompanionConfig: preserves unrelated keys, honors ARCHIVIST_COMPANION_CONFIG", () => {
  const custom = "/tmp/cfg.json";
  const fs = memFs({ [custom]: JSON.stringify({ captureModel: "sonnet", serverUrl: "old", apiKey: "old" }) });
  const r = writeCompanionConfig({
    serverUrl: "https://new/mcp/", apiKey: "arc_new",
    env: { ARCHIVIST_COMPANION_CONFIG: custom }, home: "/home/u", fs,
  });
  assert.equal(r.path, custom);
  const parsed = JSON.parse(fs.files[custom]);
  assert.equal(parsed.captureModel, "sonnet"); // preserved
  assert.equal(parsed.serverUrl, "https://new/mcp/"); // overwritten
  assert.equal(parsed.apiKey, "arc_new");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd companion && node --test test/connect.test.js`
Expected: FAIL — `writeCompanionConfig` not exported.

- [ ] **Step 3: Write minimal implementation**

Add to `companion/lib/connect.js`:

```js
export function writeCompanionConfig({ serverUrl, apiKey, env = process.env, home, fs = nodeFs }) {
  const path = env.ARCHIVIST_COMPANION_CONFIG || join(home, ".archivist-companion.json");
  let cur = {};
  if (fs.existsSync(path)) {
    try { cur = JSON.parse(fs.readFileSync(path, "utf8")); } catch { cur = {}; }
  }
  const next = { ...cur, serverUrl, apiKey };
  fs.writeFileSync(path, JSON.stringify(next, null, 2) + "\n");
  return { path };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd companion && node --test test/connect.test.js`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add companion/lib/connect.js companion/test/connect.test.js
git commit -m "feat(companion): write/merge ~/.archivist-companion.json"
```

---

### Task 7: Orchestration `run()` and summary

**Files:**
- Modify: `companion/lib/connect.js` (add `run`)
- Test: `companion/test/connect.test.js`

**Interfaces:**
- Consumes: `parseArgs`, `verify`, `registerClaudeMcp`, `writeAgentConfigs`, `installCompanion`, `writeCompanionConfig`.
- Produces: `async run({ argv, deps }) => number` (process exit code: 0 success, 1 on verify failure/missing required args). `deps` is an optional injection bag for tests: `{ verify, registerClaudeMcp, writeAgentConfigs, installCompanion, writeCompanionConfig, log, home, cwd, env }`.

**Rule enforced here:** if `verify` returns `ok:false`, `run` prints the error and returns `1` **without calling any writer** (registerClaudeMcp / writeAgentConfigs / installCompanion / writeCompanionConfig).

- [ ] **Step 1: Write the failing test**

Append to `companion/test/connect.test.js`:

```js
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
      installCompanion: rec("installCompanion", { ok: true, error: null }),
      writeCompanionConfig: rec("writeCompanionConfig", { path: "/home/u/.archivist-companion.json" }),
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
    "verify", "registerClaudeMcp", "writeAgentConfigs", "installCompanion", "writeCompanionConfig",
  ]);
});

test("run: verify failure returns 1 and calls NO writer", async () => {
  const { calls, deps } = spyDeps();
  deps.verify = () => Promise.resolve({ ok: false, team: null, error: "MCP HTTP 401" });
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd companion && node --test test/connect.test.js`
Expected: FAIL — `run` not exported.

- [ ] **Step 3: Write minimal implementation**

Add to `companion/lib/connect.js` (imports at top):

```js
import { homedir } from "node:os";
```

```js
export async function run({ argv, deps = {} }) {
  const log = deps.log || ((m) => process.stdout.write(m + "\n"));
  const home = deps.home || homedir();
  const cwd = deps.cwd || process.cwd();
  const env = deps.env || process.env;
  const _verify = deps.verify || verify;
  const _registerClaudeMcp = deps.registerClaudeMcp || registerClaudeMcp;
  const _writeAgentConfigs = deps.writeAgentConfigs || writeAgentConfigs;
  const _installCompanion = deps.installCompanion || installCompanion;
  const _writeCompanionConfig = deps.writeCompanionConfig || writeCompanionConfig;

  const args = parseArgs(argv);
  if (args.help) { log(USAGE); return 0; }

  let { server, key } = args;
  if ((!server || !key) && !args.yes) {
    // Minimal interactive fallback (skipped in tests, which always pass flags).
    const { createInterface } = await import("node:readline/promises");
    const rl = createInterface({ input: process.stdin, output: process.stdout });
    if (!server) server = (await rl.question("Archivist server URL: ")).trim();
    if (!key) key = (await rl.question("Team API key (arc_...): ")).trim();
    rl.close();
  }
  if (!server || !key) {
    log("error: --server and --key are required (or run without --yes to be prompted).");
    return 1;
  }

  const v = await _verify({ serverUrl: server, apiKey: key, cwd });
  if (!v.ok) {
    log(`error: could not verify ${server}: ${v.error}`);
    log("nothing was changed. check the URL and key, then re-run.");
    return 1;
  }
  log(`✓ connected${v.team ? ` (team: ${v.team})` : ""}`);

  const mcp = _registerClaudeMcp({ serverUrl: server, apiKey: key, scope: args.scope });
  if (mcp.ok) log(`✓ registered archivist MCP in Claude Code (scope: ${args.scope})`);
  else if (mcp.skipped) log("• claude CLI not found — skipped Claude Code MCP registration");
  else log(`• Claude Code MCP registration failed: ${mcp.error}`);

  const agents = _writeAgentConfigs({ serverUrl: server, apiKey: key, home });
  if (agents.codex) log("✓ added archivist to ~/.codex/config.toml (export ARCHIVIST_TOKEN to use it)");
  if (agents.cursor) log("✓ added archivist to ~/.cursor/mcp.json");

  if (args.companion) {
    const inst = _installCompanion({ scope: args.scope });
    if (inst.ok) log("✓ installed archivist-companion plugin");
    else log(`• companion plugin install skipped: ${inst.error}`);
    const cfg = _writeCompanionConfig({ serverUrl: server, apiKey: key, env, home });
    log(`✓ wrote companion config: ${cfg.path}`);
  }

  log("\nDone. Verify with: claude mcp list");
  return 0;
}
```

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `cd companion && node --test`
Expected: PASS — all connect tests plus the pre-existing companion tests.

- [ ] **Step 5: Commit**

```bash
git add companion/lib/connect.js companion/test/connect.test.js
git commit -m "feat(companion): orchestrate connect flow with fail-fast verify gate"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md` (Quick start)
- Modify: `companion/README.md` (One-command setup section)
- Create: `docs/agent-commands.md`
- Modify: `docs/self-hosting.md` (hand-off note)
- Modify: `CLAUDE.md` (link to docs/agent-commands.md in the docs list)

**Interfaces:** none (docs only).

- [ ] **Step 1: Rewrite README Quick start**

In `README.md`, replace the body of the "## Quick start — connect your agent" section (the three manual snippets) with the one-command flow, keeping the manual snippets under an "Advanced / manual" subheading. New primary content:

```markdown
## Quick start — connect your agent

Your administrator gives you two things: the server URL (`https://<domain>/mcp/`)
and a team **API key** (`arc_…`, a secret — treat it like a password). Then run:

```bash
npx archivist-connect --server https://<domain>/mcp/ --key arc_xxx
```

This verifies the token against the server, registers the MCP in Claude Code,
auto-detects Codex/Cursor and wires them too, installs the companion plugin, and
writes its config. Verify with `claude mcp list`.

<details><summary>Advanced / manual setup (per client)</summary>

... (keep the existing claude mcp add / Codex / Cursor snippets here) ...

</details>
```

- [ ] **Step 2: Add companion One-command setup section**

At the top of `companion/README.md` (after the intro paragraph, before "## What it does"), add:

```markdown
## One-command setup

```bash
npx archivist-connect --server https://<domain>/mcp/ --key arc_xxx
```

| Flag | Default | Meaning |
|---|---|---|
| `--server <url>` | prompted | Archivist MCP endpoint |
| `--key <arc_...>` | prompted | Team API key |
| `--scope user\|project\|local` | `user` | Claude Code registration scope |
| `--no-companion` | off | Register MCP only; skip companion install + config |
| `--yes` | off | Non-interactive (CI): fail instead of prompting |

It verifies the token first and writes nothing if verification fails. Re-running
is safe (idempotent). The manual steps below remain available if you prefer them.
```

- [ ] **Step 3: Create docs/agent-commands.md**

Create `docs/agent-commands.md`:

```markdown
# Agent commands reference

Everything a developer invokes on their coding agent once connected to Archivist.

## Setup

| Command | What it does |
|---|---|
| `npx archivist-connect --server <url> --key <arc_...>` | Verify the token, register the MCP in Claude Code (auto-detect Codex/Cursor), install + configure the companion. See flags in [companion/README](../companion/README.md#one-command-setup). |

## MCP tools (called by the agent during a session)

The agent calls these automatically per the protocol shipped in the MCP
handshake; you rarely call them by hand. Full contract: [docs/mcp-tools.md](mcp-tools.md).

| Tool | Purpose |
|---|---|
| `memory_search` | Exact-keyword + full-text search; `intent="read"\|"write"`, `scope="all"` for cross-project. |
| `memory_get` | Fetch one keyword's full value (+ pending fragments). |
| `memory_append` | Queue a refined fragment onto an existing keyword. Never blocks. |
| `memory_upsert` | Create a keyword, or write a compacted value (optimistic lock). |
| `memory_list` | List a project's keywords to orient. |
| `memory_delete` | Remove a keyword. |

## Claude Code companion

| Command | What it does |
|---|---|
| `/archivist-companion:sweep [project-slug]` | Compact all overdue keywords for the project (in-session). |
| `node companion/scripts/sweep-cli.js` | Same sweep, out-of-session, for cron / Task Scheduler. Prints `{ slug, compacted, failed }`. |

The companion also runs two hooks automatically: **SessionStart** injects a
keyword index for the current project, and **SessionEnd** auto-captures
substantive learnings. Configure via `~/.archivist-companion.json` — see the
[companion README](../companion/README.md#configuration).
```

- [ ] **Step 4: Add self-hosting hand-off note**

In `docs/self-hosting.md`, right after the `key-issue` command block, add:

```markdown
Hand the developer just two values — the server URL and the issued key. They run
`npx archivist-connect --server <url> --key <arc_...>` to wire up their agent in
one step (see [agent commands](agent-commands.md)).
```

- [ ] **Step 5: Link the new doc from CLAUDE.md**

In `CLAUDE.md`, in the intro docs list (near the mention of `docs/mcp-tools.md`), add a reference to `docs/agent-commands.md` — e.g. append to the existing docs sentence: "and the agent-side commands ([docs/agent-commands.md](docs/agent-commands.md))".

- [ ] **Step 6: Commit**

```bash
git add README.md companion/README.md docs/agent-commands.md docs/self-hosting.md CLAUDE.md
git commit -m "docs: one-command archivist-connect setup + agent commands reference"
```

---

## Final verification (manual, after all tasks)

- [ ] Run `cd companion && node --test` — full suite green (pre-existing + new).
- [ ] Run `cd companion && node scripts/connect.js --help` — prints usage, exits 0.
- [ ] **Live end-to-end** against `https://archivist.schengatto.cloud/mcp/` with a real (throwaway) key:
  - `node companion/scripts/connect.js --server https://archivist.schengatto.cloud/mcp/ --key <arc_...>`
  - Confirm `claude mcp list` shows `archivist`.
  - Confirm `~/.archivist-companion.json` has `serverUrl` + `apiKey`.
  - Start a Claude Code session; confirm the SessionStart index injects.
  - Revoke the throwaway key afterward (`create_api_key.py key-revoke --key-id <id>`).
- [ ] Update memory (`MEMORY.md` + claude-mem) per project workflow rules.

## Self-Review (completed by plan author)

- **Spec coverage:** command shape + flags (T1, T7) ✓; verify fail-fast (T2, T7 gate) ✓; Claude Code MCP register idempotent (T3) ✓; auto-detect Codex/Cursor (T4) ✓; companion install Approach A + marketplace.json fix (T5) ✓; companion config merge (T6) ✓; docs incl. `docs/agent-commands.md` (T8) ✓; testing incl. "no writes on verify failure" (T7) ✓; live e2e (final verification) ✓. No new server work — honored (nothing in the plan touches `api/`).
- **Placeholder scan:** none — every code step shows full code. (T7 test includes a deliberately-labeled messy draft immediately replaced by the "clean" version with an instruction to delete the draft; not a placeholder.)
- **Type consistency:** `runClaude` return shape `{ notFound, status, stdout, stderr }` consistent across T3/T5; `verify` returns `{ ok, team, error }` consistent T2/T7; `registerClaudeMcp` `{ ok, skipped, error }` consistent T3/T7; `installCompanion` `{ ok, error }` consistent T5/T7; `writeAgentConfigs` `{ codex, cursor }` consistent T4/T7; `writeCompanionConfig` `{ path }` consistent T6/T7.
```
