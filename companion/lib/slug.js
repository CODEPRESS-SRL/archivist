import { execFileSync } from "node:child_process";
import { basename } from "node:path";

export function slugify(name) {
  return String(name)
    .toLowerCase()
    .replace(/\.git$/, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function defaultExec(cmd, args) {
  return execFileSync(cmd, args, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
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
