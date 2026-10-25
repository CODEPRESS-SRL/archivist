import { execFileSync } from "node:child_process";
import { basename } from "node:path";
import { pathOnlyEnv, resolveFromPathOnly } from "./exe-path.js";

export function slugify(name) {
  return String(name)
    .toLowerCase()
    .replace(/\.git$/, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

/**
 * Runs `cmd` with `args` and returns its stdout, resolving `cmd` from PATH
 * only (never from the current directory).
 *
 * @param {string} cmd Executable name.
 * @param {string[]} args Arguments.
 * @param {typeof execFileSync} [execFile] Injection point for tests.
 * @returns {string}
 */
export function defaultExec(cmd, args, execFile = execFileSync) {
  return resolveFromPathOnly(() =>
    execFile(cmd, args, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"], env: pathOnlyEnv() }));
}

// Derive the Archivist project slug: git remote repo name (normalized), else dir name.
export function deriveSlug(cwd, exec = defaultExec) {
  let remote = "";
  try {
    remote = exec("git", ["-C", cwd, "remote", "get-url", "origin"]).trim();
  } catch {
    remote = "";
  }
  if (remote) {
    const repo = remote.replace(/\.git$/, "").split(/[/:]/).filter(Boolean).pop();
    if (repo) return slugify(repo);
  }
  return slugify(basename(cwd));
}
