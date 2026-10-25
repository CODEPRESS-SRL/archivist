import * as nodeFs from "node:fs";
import { homedir } from "node:os";
import { resolvePaths, describePaths } from "./paths.js";
import { runStartupGc } from "./gc.js";

export const USAGE = `archivist — inspect and clean what Archivist writes to disk.

Usage:
  archivist paths          Print every path Archivist writes to, and its state
  archivist purge [--yes]  Delete the whole Archivist directory (API key included)
  archivist gc             Run the startup garbage collection now

Everything Archivist persists lives under a single root:
  Windows  %LOCALAPPDATA%\\Archivist
  macOS    ~/Library/Application Support/Archivist
  Linux    $XDG_DATA_HOME/archivist  (fallback ~/.local/share/archivist)

Set ARCHIVIST_HOME to relocate that root; it is the only override.
`;

function dirSize(target, fs) {
  let total = 0;
  let stack = [target];
  while (stack.length) {
    const cur = stack.pop();
    let st;
    try {
      st = fs.statSync(cur);
    } catch {
      continue;
    }
    if (!st.isDirectory()) {
      total += st.size;
      continue;
    }
    try {
      for (const name of fs.readdirSync(cur)) stack.push(`${cur}/${name}`);
    } catch {
      // unreadable subtree: skip
    }
  }
  return total;
}

export function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

export function pathsReport({ paths, fs = nodeFs, env = process.env }) {
  const lines = [];
  if (env.ARCHIVIST_HOME) lines.push(`ARCHIVIST_HOME=${env.ARCHIVIST_HOME} (root relocated)`);
  for (const { label, path, note } of describePaths(paths)) {
    const exists = fs.existsSync(path);
    const size = exists ? ` (${formatBytes(dirSize(path, fs))})` : " (absent)";
    lines.push(`${label.padEnd(7)} ${path}${size}`);
    lines.push(`${" ".repeat(8)}${note}`);
  }
  lines.push("");
  lines.push("Written outside this root (owned by other tools, left in place by purge):");
  lines.push(`  ${paths.legacyConfigFile}  pre-1.1 config, migrated on next connect`);
  lines.push("  ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md   'archivist:' rule block");
  lines.push("  ~/.codex/config.toml, ~/.cursor/mcp.json  MCP server registration");
  lines.push("  ~/.claude/plugins/…                       companion plugin (claude plugin uninstall)");
  return lines.join("\n");
}

export function purge({ paths, fs = nodeFs }) {
  const removed = [];
  // The whole cache root, not just our subdirectory: purge means "remove
  // everything Archivist put on this machine".
  for (const target of [paths.data, paths.cache]) {
    if (!fs.existsSync(target)) continue;
    try {
      fs.rmSync(target, { recursive: true, force: true });
      removed.push(target);
    } catch {
      // reported as not removed
    }
  }
  return { removed };
}

export async function run({ argv, deps = {} }) {
  const out = deps.log || ((m) => process.stdout.write(m + "\n"));
  const env = deps.env || process.env;
  const home = deps.home || homedir();
  const fs = deps.fs || nodeFs;
  const confirm = deps.confirm || defaultConfirm;
  const paths = resolvePaths({ env, home });
  const [cmd, ...rest] = argv;

  switch (cmd) {
    case "paths":
      out(pathsReport({ paths, fs, env }));
      return 0;
    case "gc": {
      const { temp, cache } = runStartupGc({ paths, fs });
      out(`gc: removed ${temp.removed} stale temp dir(s), ${cache.removed} cache entr(y|ies)`);
      return 0;
    }
    case "purge": {
      if (!rest.includes("--yes")) {
        out(`This deletes ${paths.data}`);
        if (paths.cache !== paths.data) out(`and ${paths.cache}`);
        out("including the stored API key. This cannot be undone.");
        const ok = await confirm("Type 'purge' to confirm: ");
        if (ok !== "purge") {
          out("aborted; nothing was removed. (use --yes for a non-interactive purge)");
          return 1;
        }
      }
      const { removed } = purge({ paths, fs });
      if (removed.length === 0) out("nothing to remove — no Archivist directory on this machine.");
      for (const p of removed) out(`removed ${p}`);
      out("Note: the MCP registration and rule blocks in your agents' own config");
      out("files are left untouched — see `archivist paths` for that list.");
      return 0;
    }
    case "-h":
    case "--help":
    case undefined:
      out(USAGE);
      return 0;
    default:
      out(`unknown command: ${cmd}\n`);
      out(USAGE);
      return 2;
  }
}

// Returns "" when the answer cannot be read — a closed stdin (`archivist purge
// < /dev/null`, a CI runner, a pipe) must abort, never silently succeed.
async function defaultConfirm(question) {
  const { createInterface } = await import("node:readline/promises");
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  try {
    const answer = await new Promise((resolve) => {
      rl.question(question).then((a) => resolve(a), () => resolve(""));
      rl.once("close", () => resolve(""));
    });
    return answer.trim();
  } finally {
    rl.close();
  }
}
