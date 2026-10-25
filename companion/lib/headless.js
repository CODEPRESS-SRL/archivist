import { spawn as nodeSpawn } from "node:child_process";
import * as nodeFs from "node:fs";
import { join } from "node:path";
import { resolvePaths } from "./paths.js";
import { createTempDir, removeTempDir } from "./tempdir.js";
import { pathOnlyEnv, resolveFromPathOnly } from "./exe-path.js";

// JSON for `claude --mcp-config`: a single remote streamable-HTTP Archivist
// server authenticated with the team Bearer key.
export function mcpConfigJson(cfg) {
  return JSON.stringify({
    mcpServers: {
      archivist: {
        type: "http",
        url: cfg.serverUrl,
        headers: { Authorization: `Bearer ${cfg.apiKey}` },
      },
    },
  });
}

// Stage the config as a file under the Archivist temp root instead of passing
// it inline: the inline form puts the team API key in the child's command line,
// where any local process can read it out of `ps` / Task Manager.
//
// `register` mirrors the spawn mode. A detached child outlives us and must keep
// reading the file, so its directory is left for the startup GC (which only
// reclaims it once this PID is gone and it is hours old); an awaited child lets
// us delete deterministically.
export function stageMcpConfig(cfg, { paths = resolvePaths(), fs = nodeFs, register = true } = {}) {
  const json = mcpConfigJson(cfg);
  try {
    const dir = createTempDir({ paths, register });
    const file = join(dir, "mcp-config.json");
    fs.writeFileSync(file, json, { mode: 0o600 });
    return { value: file, dir };
  } catch {
    // Unwritable root: fall back to the inline form rather than lose the run.
    return { value: json, dir: null };
  }
}

export function buildArgs({ prompt, mcpConfig, allowedTools, model, maxTurns }) {
  return [
    "-p", prompt,
    "--mcp-config", mcpConfig,
    "--strict-mcp-config",
    "--allowedTools", allowedTools,
    "--model", model,
    "--max-turns", String(maxTurns),
    "--output-format", "json",
  ];
}

// Spawn a headless `claude -p` run against Archivist. The child always carries
// ARCHIVIST_HEADLESS=1 so its own SessionEnd/SessionStart hooks no-op (no
// recursion). detached:true fire-and-forgets (used at session end so teardown
// is never delayed); detached:false awaits the child (used by the sweep CLI).
export function runHeadless({
  cfg,
  prompt,
  allowedTools,
  model,
  maxTurns,
  detached = false,
  spawn = nodeSpawn,
  paths = resolvePaths(),
  fs = nodeFs,
}) {
  const staged = stageMcpConfig(cfg, { paths, fs, register: !detached });
  const args = buildArgs({ prompt, mcpConfig: staged.value, allowedTools, model, maxTurns });
  const env = pathOnlyEnv({ ...process.env, ARCHIVIST_HEADLESS: "1" });
  if (detached) {
    const child = resolveFromPathOnly(() => spawn("claude", args, { detached: true, stdio: "ignore", env }));
    // A ChildProcess is an EventEmitter: an async spawn failure (e.g. ENOENT
    // when `claude` isn't on PATH) emits 'error', which THROWS if unhandled and
    // would crash the host session during teardown. Swallow it to stay
    // fail-silent — a fire-and-forget run must never break session teardown.
    if (child && typeof child.on === "function") child.on("error", () => {});
    if (child && typeof child.unref === "function") child.unref();
    return Promise.resolve({ detached: true });
  }
  return new Promise((resolve) => {
    const done = (result) => {
      if (staged.dir) removeTempDir(staged.dir);
      resolve(result);
    };
    const child = resolveFromPathOnly(() => spawn("claude", args, { stdio: "ignore", env }));
    child.on("close", (code) => done({ code }));
    child.on("error", () => done({ error: true }));
  });
}
