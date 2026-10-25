import * as nodeFs from "node:fs";
import { dirname } from "node:path";
import { resolvePaths } from "./paths.js";

// Diagnostics go to stderr, never stdout: for a SessionStart hook stdout IS
// the data channel (the injected index), and the MCP spec reserves stdout for
// protocol traffic. Off by default so the fail-silent contract holds; opt in
// with ARCHIVIST_DEBUG=1.
//
// A log FILE is written only when ARCHIVIST_LOG_FILE=1, lives under the
// Archivist root, and is rotated at MAX_BYTES with a single .1 backup — so the
// on-disk cost is bounded at 2 * MAX_BYTES, forever.

export const MAX_BYTES = 1024 * 1024;

function enabled(env, name) {
  const v = env[name];
  return v === "1" || v === "true";
}

export function rotateIfNeeded(file, { maxBytes = MAX_BYTES, fs = nodeFs } = {}) {
  try {
    if (fs.statSync(file).size < maxBytes) return false;
  } catch {
    return false; // no file yet
  }
  try {
    fs.rmSync(`${file}.1`, { force: true });
    fs.renameSync(file, `${file}.1`);
    return true;
  } catch {
    return false;
  }
}

export function log(message, { env = process.env, paths = null, fs = nodeFs, stderr = process.stderr } = {}) {
  const line = `[archivist] ${new Date().toISOString()} ${message}\n`;
  if (enabled(env, "ARCHIVIST_DEBUG")) {
    try {
      stderr.write(line);
    } catch {
      // never let logging break the caller
    }
  }
  if (!enabled(env, "ARCHIVIST_LOG_FILE")) return;
  try {
    const file = (paths || resolvePaths({ env })).logFile;
    fs.mkdirSync(dirname(file), { recursive: true });
    rotateIfNeeded(file, { fs });
    fs.appendFileSync(file, line);
  } catch {
    // fail-silent by contract
  }
}
