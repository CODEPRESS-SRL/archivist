import { spawn as nodeSpawn } from "node:child_process";

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
export function runHeadless({ cfg, prompt, allowedTools, model, maxTurns, detached = false, spawn = nodeSpawn }) {
  const args = buildArgs({ prompt, mcpConfig: mcpConfigJson(cfg), allowedTools, model, maxTurns });
  const env = { ...process.env, ARCHIVIST_HEADLESS: "1" };
  if (detached) {
    const child = spawn("claude", args, { detached: true, stdio: "ignore", env });
    // A ChildProcess is an EventEmitter: an async spawn failure (e.g. ENOENT
    // when `claude` isn't on PATH) emits 'error', which THROWS if unhandled and
    // would crash the host session during teardown. Swallow it to stay
    // fail-silent — a fire-and-forget run must never break session teardown.
    if (child && typeof child.on === "function") child.on("error", () => {});
    if (child && typeof child.unref === "function") child.unref();
    return Promise.resolve({ detached: true });
  }
  return new Promise((resolve) => {
    const child = spawn("claude", args, { stdio: "ignore", env });
    child.on("close", (code) => resolve({ code }));
    child.on("error", () => resolve({ error: true }));
  });
}
