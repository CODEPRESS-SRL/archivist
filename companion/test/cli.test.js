import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { resolvePaths } from "../lib/paths.js";
import { run, pathsReport, purge, formatBytes, USAGE } from "../lib/cli.js";

function sandbox({ populate = true } = {}) {
  const home = mkdtempSync(join(tmpdir(), "arc-cli-"));
  const env = { ARCHIVIST_HOME: join(home, "root") };
  const paths = resolvePaths({ env, home });
  if (populate) {
    mkdirSync(paths.tmp, { recursive: true });
    mkdirSync(paths.cache, { recursive: true });
    writeFileSync(paths.configFile, JSON.stringify({ serverUrl: "https://x/mcp/", apiKey: "arc_k" }));
  }
  return { env, home, paths };
}

function capture() {
  const lines = [];
  return { lines, log: (m) => lines.push(m), text: () => lines.join("\n") };
}

test("formatBytes is readable at every magnitude", () => {
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(2048), "2.0 KB");
  assert.equal(formatBytes(5 * 1024 * 1024), "5.0 MB");
});

test("paths prints every writable location plus what lives outside the root", async () => {
  const { env, home, paths } = sandbox();
  const out = capture();
  const code = await run({ argv: ["paths"], deps: { env, home, log: out.log } });
  assert.equal(code, 0);
  const text = out.text();
  for (const p of [paths.data, paths.cache, paths.tmp, paths.logs, paths.configFile]) {
    assert.ok(text.includes(p), `report must mention ${p}`);
  }
  assert.match(text, /ARCHIVIST_HOME=/);
  assert.match(text, /Written outside this root/);
  assert.match(text, /\.claude\/CLAUDE\.md/);
  assert.match(text, /plugin uninstall/);
});

test("paths marks absent directories instead of creating them", async () => {
  const { env, home, paths } = sandbox({ populate: false });
  const out = capture();
  await run({ argv: ["paths"], deps: { env, home, log: out.log } });
  assert.match(out.text(), /\(absent\)/);
  assert.ok(!existsSync(paths.data), "`archivist paths` must be read-only");
});

test("purge --yes removes the whole root", async () => {
  const { env, home, paths } = sandbox();
  const out = capture();
  const code = await run({ argv: ["purge", "--yes"], deps: { env, home, log: out.log } });
  assert.equal(code, 0);
  assert.ok(!existsSync(paths.data));
  assert.ok(!existsSync(paths.configFile));
  assert.match(out.text(), /left untouched/, "must say what purge does NOT remove");
});

test("purge asks for confirmation and aborts on anything else", async () => {
  const { env, home, paths } = sandbox();
  const out = capture();
  const code = await run({
    argv: ["purge"],
    deps: { env, home, log: out.log, confirm: async () => "no" },
  });
  assert.equal(code, 1);
  assert.ok(existsSync(paths.configFile), "nothing removed after an aborted confirmation");
  assert.match(out.text(), /aborted/);
});

test("purge proceeds when the confirmation matches", async () => {
  const { env, home, paths } = sandbox();
  const out = capture();
  const code = await run({
    argv: ["purge"],
    deps: { env, home, log: out.log, confirm: async () => "purge" },
  });
  assert.equal(code, 0);
  assert.ok(!existsSync(paths.data));
});

test("purge on a clean machine says so instead of failing", async () => {
  const { env, home } = sandbox({ populate: false });
  const out = capture();
  const code = await run({ argv: ["purge", "--yes"], deps: { env, home, log: out.log } });
  assert.equal(code, 0);
  assert.match(out.text(), /nothing to remove/);
});

test("purge leaves a directory it cannot remove without throwing", () => {
  const { paths } = sandbox();
  const fs = {
    existsSync: () => true,
    rmSync: () => { throw new Error("EBUSY"); },
  };
  assert.doesNotThrow(() => {
    const r = purge({ paths, fs });
    assert.deepEqual(r.removed, []);
  });
});

test("gc runs the sweep and reports counts", async () => {
  const { env, home } = sandbox();
  const out = capture();
  const code = await run({ argv: ["gc"], deps: { env, home, log: out.log } });
  assert.equal(code, 0);
  assert.match(out.text(), /^gc: removed \d+ stale temp dir/m);
});

test("no command prints usage; an unknown one exits 2", async () => {
  const { env, home } = sandbox();
  const help = capture();
  assert.equal(await run({ argv: [], deps: { env, home, log: help.log } }), 0);
  assert.equal(help.text(), USAGE);

  const bad = capture();
  assert.equal(await run({ argv: ["frobnicate"], deps: { env, home, log: bad.log } }), 2);
  assert.match(bad.text(), /unknown command/);
});

test("pathsReport reports sizes for populated directories", () => {
  const { env, home, paths } = sandbox();
  writeFileSync(join(paths.cache, "blob.bin"), "x".repeat(4096));
  const text = pathsReport({ paths, env });
  assert.match(text, /4\.0 KB/);
});

test("purge aborts (exit 1) when the confirmation cannot be read at all", async () => {
  // `archivist purge < /dev/null`, a CI runner, a pipe: a confirmation that
  // never arrives must never be reported as a successful purge.
  const { env, home, paths } = sandbox();
  const out = capture();
  const code = await run({
    argv: ["purge"],
    deps: { env, home, log: out.log, confirm: async () => "" },
  });
  assert.equal(code, 1);
  assert.ok(existsSync(paths.configFile), "nothing removed");
  assert.match(out.text(), /--yes for a non-interactive purge/);
});
