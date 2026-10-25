import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  statSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, isAbsolute } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { resolvePaths } from "../lib/paths.js";

// Non-regression guarantee for third-party installs: after a full request
// cycle followed by a SIGKILL, (a) nothing exists outside the declared
// Archivist root, and (b) whatever the kill stranded is reclaimed on the next
// startup.

const __dirname = dirname(fileURLToPath(import.meta.url));
const SESSION_START = join(__dirname, "..", "scripts", "session-start.js");
const TEMPDIR_LIB = pathToFileURL(join(__dirname, "..", "lib", "tempdir.js")).href;

function listTree(root) {
  const found = [];
  if (!existsSync(root)) return found;
  const stack = [root];
  while (stack.length) {
    const cur = stack.pop();
    let st;
    try {
      st = statSync(cur);
    } catch {
      continue;
    }
    if (st.isDirectory()) {
      for (const name of readdirSync(cur)) stack.push(join(cur, name));
    }
    if (cur !== root) found.push(cur);
  }
  return found;
}

function under(parent, child) {
  const rel = relative(parent, child);
  return rel !== "" && !rel.startsWith("..") && !isAbsolute(rel);
}

// A minimal stateless streamable-HTTP MCP stand-in — enough for the client's
// initialize -> notifications/initialized -> tools/call handshake.
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
          res.writeHead(202).end();
          return;
        }
        const result =
          msg.method === "tools/call"
            ? { structuredContent: { entries: [{ keyword: "deploy", compaction_due: false }] } }
            : { protocolVersion: "2024-11-05", capabilities: {}, serverInfo: { name: "mock", version: "1" } };
        res.writeHead(200, { "Content-Type": "text/event-stream" });
        res.end(`event: message\ndata: ${JSON.stringify({ jsonrpc: "2.0", id: msg.id, result })}\n\n`);
      });
    });
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

// An isolated machine: fake home, fake working directory, declared root.
function fakeMachine() {
  const base = mkdtempSync(join(tmpdir(), "arc-footprint-"));
  const home = join(base, "home");
  const cwd = join(base, "cwd");
  const root = join(base, "root");
  mkdirSync(home, { recursive: true });
  mkdirSync(cwd, { recursive: true });
  const paths = resolvePaths({ env: { ARCHIVIST_HOME: root }, home });
  return { base, home, cwd, root, paths };
}

// Async on purpose: spawnSync would block this process's event loop, and the
// mock MCP server runs here — it would never get to answer the child.
function runHook(script, { input, env, cwd, timeout = 20000 }) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [script], { env, cwd });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`hook timed out after ${timeout}ms`));
    }, timeout);
    child.on("error", reject);
    child.on("close", (status) => {
      clearTimeout(timer);
      resolve({ status, stdout, stderr });
    });
    child.stdin.end(input);
  });
}

function childEnv(machine, extra = {}) {
  // Drop any inherited Archivist config FIRST, then layer this test's values on
  // top — deleting afterwards would wipe the very env the test is setting.
  const env = { ...process.env };
  delete env.ARCHIVIST_SERVER_URL;
  delete env.ARCHIVIST_API_KEY;
  env.ARCHIVIST_HOME = machine.root;
  env.HOME = machine.home;
  env.USERPROFILE = machine.home;
  return { ...env, ...extra };
}

test("a full request cycle writes nothing outside the declared root", async () => {
  const machine = fakeMachine();
  const server = await startMockMcpServer();
  const { port } = server.address();
  const tmpBefore = new Set(readdirSync(tmpdir()));
  try {
    const res = await runHook(SESSION_START, {
      input: JSON.stringify({ source: "startup", cwd: machine.cwd }),
      env: childEnv(machine, {
        ARCHIVIST_SERVER_URL: `http://127.0.0.1:${port}/mcp/`,
        ARCHIVIST_API_KEY: "test-key",
      }),
      cwd: machine.cwd,
    });
    assert.equal(res.status, 0, `hook must exit clean; stderr: ${res.stderr}`);
    assert.match(res.stdout, /deploy/, "the cycle really ran (index injected)");

    // Nothing in the working directory, nothing in the root of the home.
    assert.deepEqual(listTree(machine.cwd), [], "must never write to the working directory");
    for (const path of listTree(machine.home)) {
      assert.ok(under(machine.root, path), `wrote outside the root: ${path}`);
    }
    // Nothing seeded into the OS temp dir either.
    const strays = readdirSync(tmpdir()).filter((n) => !tmpBefore.has(n) && n.startsWith("archivist-"));
    assert.deepEqual(strays, [], "temporaries belong under the Archivist root, not %TEMP%");
  } finally {
    server.close();
  }
});

test("a SIGKILLed session strands a temp dir, and the next startup reclaims it", async () => {
  const machine = fakeMachine();

  // A stand-in for a hook that is killed mid-run: it stages a temp dir the way
  // runHeadless does, then blocks forever.
  const victim = join(machine.base, "victim.mjs");
  writeFileSync(
    victim,
    `import { createTempDir } from ${JSON.stringify(TEMPDIR_LIB)};\n` +
      `import { resolvePaths } from ${JSON.stringify(pathToFileURL(join(__dirname, "..", "lib", "paths.js")).href)};\n` +
      `const paths = resolvePaths();\n` +
      `const dir = createTempDir({ paths });\n` +
      `process.stdout.write(dir + "\\n");\n` +
      `setInterval(() => {}, 1000);\n`
  );

  const child = spawn(process.execPath, [victim], { env: childEnv(machine), cwd: machine.cwd });
  const staged = await new Promise((resolve, reject) => {
    let out = "";
    child.stdout.on("data", (c) => {
      out += c;
      if (out.includes("\n")) resolve(out.trim());
    });
    child.on("error", reject);
    setTimeout(() => reject(new Error("victim never reported its temp dir")), 15000).unref();
  });

  assert.ok(existsSync(staged));
  assert.ok(under(machine.paths.tmp, staged), "staged under the Archivist temp root");

  // SIGKILL: no exit handler, no signal handler, no chance to clean up.
  child.kill("SIGKILL");
  await new Promise((resolve) => child.on("exit", resolve));
  assert.ok(existsSync(staged), "a killed process cannot clean up after itself — that is the point");

  // Age it past the GC threshold, the way a next-day restart would see it.
  const old = new Date(Date.now() - 24 * 3600000);
  utimesSync(staged, old, old);

  const server = await startMockMcpServer();
  const { port } = server.address();
  try {
    const res = await runHook(SESSION_START, {
      input: JSON.stringify({ source: "startup", cwd: machine.cwd }),
      env: childEnv(machine, {
        ARCHIVIST_SERVER_URL: `http://127.0.0.1:${port}/mcp/`,
        ARCHIVIST_API_KEY: "test-key",
      }),
      cwd: machine.cwd,
    });
    assert.equal(res.status, 0);
    assert.ok(!existsSync(staged), "the next startup must reclaim the stranded temp dir");
    assert.ok(existsSync(machine.paths.tmp), "the temp root itself survives");
  } finally {
    server.close();
  }
});

test("an unconfigured hook still garbage-collects, and still writes nothing outside the root", async () => {
  const machine = fakeMachine();
  const stranded = join(machine.paths.tmp, "archivist-999999-deadbeef");
  mkdirSync(stranded, { recursive: true });
  const old = new Date(Date.now() - 48 * 3600000);
  utimesSync(stranded, old, old);

  const res = await runHook(SESSION_START, {
    input: JSON.stringify({ source: "startup", cwd: machine.cwd }),
    env: childEnv(machine),
    cwd: machine.cwd,
  });

  assert.equal(res.status, 0);
  assert.ok(!existsSync(stranded), "GC runs before the configured-or-not check");
  assert.deepEqual(listTree(machine.cwd), []);
  for (const path of listTree(machine.home)) {
    assert.ok(under(machine.root, path), `wrote outside the root: ${path}`);
  }
});
