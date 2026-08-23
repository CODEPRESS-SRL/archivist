import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { resolvePaths } from "../lib/paths.js";
import { log, rotateIfNeeded, MAX_BYTES } from "../lib/log.js";

function sandbox() {
  const home = mkdtempSync(join(tmpdir(), "arc-log-"));
  return resolvePaths({ env: { ARCHIVIST_HOME: join(home, "root") }, home });
}

function fakeStderr() {
  const written = [];
  return { written, write: (s) => written.push(s) };
}

test("silent by default: no stderr output, no file", () => {
  const paths = sandbox();
  const stderr = fakeStderr();
  log("something failed", { env: {}, paths, stderr });
  assert.deepEqual(stderr.written, []);
  assert.ok(!existsSync(paths.logFile));
});

test("ARCHIVIST_DEBUG=1 writes to stderr — never stdout", () => {
  const paths = sandbox();
  const stderr = fakeStderr();
  log("boom", { env: { ARCHIVIST_DEBUG: "1" }, paths, stderr });
  assert.equal(stderr.written.length, 1);
  assert.match(stderr.written[0], /\[archivist\].*boom/);
  assert.ok(!existsSync(paths.logFile), "debug alone must not create a log file");
});

test("a log file appears only when explicitly enabled, under the Archivist root", () => {
  const paths = sandbox();
  log("first", { env: { ARCHIVIST_LOG_FILE: "1" }, paths, stderr: fakeStderr() });
  log("second", { env: { ARCHIVIST_LOG_FILE: "1" }, paths, stderr: fakeStderr() });
  assert.ok(paths.logFile.startsWith(paths.data));
  const body = readFileSync(paths.logFile, "utf8");
  assert.match(body, /first/);
  assert.match(body, /second/);
});

test("rotation caps the log at two files, so it cannot grow forever", () => {
  const paths = sandbox();
  log("seed", { env: { ARCHIVIST_LOG_FILE: "1" }, paths, stderr: fakeStderr() });
  writeFileSync(paths.logFile, "x".repeat(MAX_BYTES + 1));

  assert.equal(rotateIfNeeded(paths.logFile), true);
  assert.ok(existsSync(`${paths.logFile}.1`));
  assert.ok(!existsSync(paths.logFile), "the live file is rotated away");

  log("after rotation", { env: { ARCHIVIST_LOG_FILE: "1" }, paths, stderr: fakeStderr() });
  assert.ok(statSync(paths.logFile).size < MAX_BYTES);

  // A second rotation replaces the single backup instead of accumulating.
  writeFileSync(paths.logFile, "y".repeat(MAX_BYTES + 1));
  rotateIfNeeded(paths.logFile);
  assert.ok(!existsSync(`${paths.logFile}.2`));
  assert.match(readFileSync(`${paths.logFile}.1`, "utf8"), /^y/);
});

test("rotateIfNeeded is a no-op below the cap and on a missing file", () => {
  const paths = sandbox();
  assert.equal(rotateIfNeeded(paths.logFile), false);
  log("tiny", { env: { ARCHIVIST_LOG_FILE: "1" }, paths, stderr: fakeStderr() });
  assert.equal(rotateIfNeeded(paths.logFile), false);
});

test("logging never throws, whatever the filesystem does", () => {
  const paths = sandbox();
  const boom = () => { throw new Error("EIO"); };
  assert.doesNotThrow(() =>
    log("x", {
      env: { ARCHIVIST_DEBUG: "1", ARCHIVIST_LOG_FILE: "1" },
      paths,
      fs: { mkdirSync: boom, statSync: boom, appendFileSync: boom, rmSync: boom, renameSync: boom },
      stderr: { write: boom },
    })
  );
});
