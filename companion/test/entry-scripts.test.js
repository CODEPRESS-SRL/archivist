import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";

// Smoke tests for the three hook/CLI entry scripts, run as real child
// processes (not imported as modules) so the recursion-guard-first and
// fail-silent contracts are verified exactly as a scheduler/hook runner
// would exercise them.

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCRIPTS = {
  "session-end.js": join(__dirname, "..", "scripts", "session-end.js"),
  "session-start.js": join(__dirname, "..", "scripts", "session-start.js"),
  "sweep-cli.js": join(__dirname, "..", "scripts", "sweep-cli.js"),
};

// No server config, and ARCHIVIST_HOME pointed at a throwaway root — the real
// config file, the real Archivist directory, and the network are never touched.
function cleanEnv(extra = {}) {
  // Drop inherited Archivist config FIRST, then layer `extra` on top: deleting
  // afterwards wiped the server URL/key the caller had just supplied, so the
  // "configured against a live MCP server" tests never actually connected.
  const env = { ...process.env };
  delete env.ARCHIVIST_SERVER_URL;
  delete env.ARCHIVIST_API_KEY;
  env.ARCHIVIST_HOME = mkdtempSync(join(tmpdir(), "arc-entry-scripts-"));
  return { ...env, ...extra };
}

// Async variant, required whenever the test also runs the mock MCP server in
// this process: spawnSync would block the event loop and the server would never
// answer the child (which then silently exercises nothing).
function runAsync(script, { input = "", env = {} } = {}, timeout = 20000) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [script], { env: cleanEnv(env) });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`timed out after ${timeout}ms`));
    }, timeout);
    child.on("error", reject);
    child.on("close", (status) => {
      clearTimeout(timer);
      resolve({ status, stdout, stderr });
    });
    child.stdin.end(input);
  });
}

function run(script, { input = "", env = {} } = {}) {
  // `input` provides stdin content and its own EOF, so session-end.js /
  // session-start.js (which read stdin to completion) never hang.
  return spawnSync(process.execPath, [script], {
    input,
    env: cleanEnv(env),
    timeout: 10000,
    encoding: "utf8",
  });
}

for (const [name, script] of Object.entries(SCRIPTS)) {
  test(`${name}: ARCHIVIST_HEADLESS=1 guard short-circuits, exit 0`, () => {
    const res = run(script, { input: "", env: { ARCHIVIST_HEADLESS: "1" } });
    assert.equal(res.status, 0);
  });
}

test("session-end.js: malformed stdin, unconfigured — fail-silent, exit 0", () => {
  const res = run(SCRIPTS["session-end.js"], { input: "{ not json" });
  assert.equal(res.status, 0);
});

test("session-start.js: malformed stdin, unconfigured — fail-silent, exit 0", () => {
  const res = run(SCRIPTS["session-start.js"], { input: "{ not json" });
  assert.equal(res.status, 0);
});

// A minimal FastMCP(stateless_http=True) stand-in: SSE `event: message` bodies
// for id-bearing requests, 202 for notifications. Just enough for the client's
// initialize -> notifications/initialized -> tools/call handshake to succeed so
// the entry scripts open (and must cleanly tear down) a real undici socket.
function startMockMcpServer() {
  return new Promise((resolve) => {
    const server = createServer((req, res) => {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", () => {
        let msg = {};
        try {
          msg = JSON.parse(body);
        } catch {
          /* ignore */
        }
        if (msg.id === undefined) {
          res.writeHead(202).end(); // notification
          return;
        }
        const result =
          msg.method === "tools/call"
            ? { structuredContent: { entries: [] } }
            : { protocolVersion: "2024-11-05", capabilities: {}, serverInfo: { name: "mock", version: "1" } };
        res.writeHead(200, { "Content-Type": "text/event-stream" });
        res.end(`event: message\ndata: ${JSON.stringify({ jsonrpc: "2.0", id: msg.id, result })}\n\n`);
      });
    });
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

// Regression: forcing process.exit(0) after an MCP call raced undici's socket
// teardown into a libuv crash (exit 127) on Windows — the script produced its
// output but the host saw a failed hook. These run the scripts configured
// against a live server so the socket is actually opened; they must still exit 0.
for (const name of ["session-start.js", "sweep-cli.js"]) {
  test(`${name}: configured against a live MCP server exits 0 (no exit/undici race)`, async () => {
    const server = await startMockMcpServer();
    const { port } = server.address();
    try {
      const res = await runAsync(SCRIPTS[name], {
        input: JSON.stringify({ source: "startup", cwd: process.cwd() }),
        env: {
          ARCHIVIST_SERVER_URL: `http://127.0.0.1:${port}/mcp/`,
          ARCHIVIST_API_KEY: "test-key",
        },
      });
      assert.equal(res.status, 0, `expected clean exit; stderr: ${res.stderr}`);
    } finally {
      server.close();
    }
  });
}
