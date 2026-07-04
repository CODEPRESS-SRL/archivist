import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
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

// No server config and ARCHIVIST_COMPANION_CONFIG pointed at a nonexistent
// path — no real config file or network is ever touched by these tests.
function cleanEnv(extra = {}) {
  const env = { ...process.env, ...extra };
  delete env.ARCHIVIST_SERVER_URL;
  delete env.ARCHIVIST_API_KEY;
  env.ARCHIVIST_COMPANION_CONFIG = join(tmpdir(), "arc-entry-scripts-nonexistent-config.json");
  return env;
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
