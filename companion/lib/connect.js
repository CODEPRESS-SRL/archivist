import { spawnSync } from "node:child_process";
import * as nodeFs from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path/posix";
import { fileURLToPath } from "node:url";
import { dirname } from "node:path";
import { createClient as realCreateClient } from "./mcp-client.js";
import { deriveSlug as realDeriveSlug } from "./slug.js";

export const USAGE = `archivist-connect — connect this machine's coding agent to an Archivist server.

Usage:
  npx archivist-connect --server <url> --key <arc_...> [options]

Options:
  --server <url>        Archivist MCP endpoint (e.g. https://host/mcp/)
  --key <arc_...>       Team API key
  --scope <s>           Claude Code scope: user | project | local (default: user)
  --no-companion        Register MCP only; skip companion install + config
  --yes                 Non-interactive; fail instead of prompting for missing values
  -h, --help            Show this help
`;

export function parseArgs(argv) {
  const out = { server: null, key: null, scope: "user", companion: true, yes: false, help: false };
  const it = [...argv];
  while (it.length) {
    let a = it.shift();
    let val = null;
    if (a.startsWith("--")) {
      const eq = a.indexOf("=");
      if (eq !== -1) { val = a.slice(eq + 1); a = a.slice(0, eq); }
    }
    const next = () => (val !== null ? val : it.shift());
    switch (a) {
      case "--server": out.server = next(); break;
      case "--key": out.key = next(); break;
      case "--scope": out.scope = next(); break;
      case "--no-companion": out.companion = false; break;
      case "--yes": out.yes = true; break;
      case "-h":
      case "--help": out.help = true; break;
      default: break;
    }
  }
  return out;
}

// Confirm the server is reachable and the token authenticates, by issuing a
// real memory_list (which forces the auth path). Never throws.
export async function verify({
  serverUrl,
  apiKey,
  cwd,
  createClient = realCreateClient,
  deriveSlug = realDeriveSlug,
}) {
  try {
    const client = createClient({ serverUrl, apiKey });
    const res = await client.memoryList(deriveSlug(cwd));
    const team = (res && (res.attribution || res.team || res.owner_name)) || null;
    return { ok: true, team, error: null };
  } catch (err) {
    return { ok: false, team: null, error: err && err.message ? err.message : String(err) };
  }
}

// Real command runner. Shell-less spawn; resolves `claude.exe`/`claude` on PATH.
// (Windows .cmd-shim caveat: same as the rest of the companion — a pure .cmd
// shim not resolvable by a shell-less spawn reports notFound.)
export function defaultRunClaude(args) {
  const r = spawnSync("claude", args, { encoding: "utf8" });
  if (r.error && r.error.code === "ENOENT") {
    return { notFound: true, status: null, stdout: "", stderr: "" };
  }
  return { notFound: false, status: r.status, stdout: r.stdout || "", stderr: r.stderr || "" };
}

export function registerClaudeMcp({ serverUrl, apiKey, scope, runClaude = defaultRunClaude }) {
  // Idempotent: remove any existing registration first (ignore its result), then add.
  const probe = runClaude(["mcp", "remove", "archivist", "-s", scope]);
  if (probe.notFound) return { ok: false, skipped: true, error: null };

  const add = runClaude([
    "mcp", "add", "--transport", "http", "archivist", serverUrl,
    "-H", `Authorization: Bearer ${apiKey}`, "-s", scope,
  ]);
  if (add.notFound) return { ok: false, skipped: true, error: null };
  if (add.status !== 0) {
    return { ok: false, skipped: false, error: add.stderr || `claude mcp add exited ${add.status}` };
  }
  return { ok: true, skipped: false, error: null };
}

// Only touches a client's config if it ALREADY exists (auto-detect). Never
// creates Codex/Cursor config from scratch; never installs the companion for them.
export function writeAgentConfigs({ serverUrl, apiKey, home, fs = nodeFs }) {
  const result = { codex: false, cursor: false };

  const codexPath = join(home, ".codex", "config.toml");
  if (fs.existsSync(codexPath)) {
    const cur = fs.readFileSync(codexPath, "utf8");
    if (!cur.includes("[mcp_servers.archivist]")) {
      const block =
        `\n[mcp_servers.archivist]\n` +
        `url = "${serverUrl}"\n` +
        `bearer_token_env_var = "ARCHIVIST_TOKEN"\n`;
      fs.writeFileSync(codexPath, cur.endsWith("\n") ? cur + block : cur + "\n" + block);
      result.codex = true;
    }
  }

  const cursorPath = join(home, ".cursor", "mcp.json");
  if (fs.existsSync(cursorPath)) {
    let json = {};
    try { json = JSON.parse(fs.readFileSync(cursorPath, "utf8")); } catch { json = {}; }
    if (!json.mcpServers) json.mcpServers = {};
    json.mcpServers.archivist = { url: serverUrl, headers: { Authorization: `Bearer ${apiKey}` } };
    fs.writeFileSync(cursorPath, JSON.stringify(json, null, 2) + "\n");
    result.cursor = true;
  }

  return result;
}

// The companion package dir (contains .claude-plugin/marketplace.json).
// lib/connect.js -> lib -> package root.
export function defaultPackageRoot() {
  return dirname(dirname(fileURLToPath(import.meta.url)));
}

// Register the shipped plugin dir as a local marketplace, then install it.
// The marketplace add is best-effort (a pre-existing marketplace is fine); only
// a failed install is a real error. Returns skipped-as-error=false on notFound
// so the orchestrator can warn without aborting.
export function installCompanion({ scope, runClaude = defaultRunClaude, packageRoot = defaultPackageRoot() }) {
  const mkt = runClaude(["plugin", "marketplace", "add", packageRoot]);
  if (mkt.notFound) return { ok: false, error: "claude CLI not found" };
  // status !== 0 is tolerated here (marketplace may already be registered).

  const inst = runClaude(["plugin", "install", "archivist-companion@archivist-tools", "-s", scope]);
  if (inst.notFound) return { ok: false, error: "claude CLI not found" };
  if (inst.status !== 0) {
    return { ok: false, error: inst.stderr || `claude plugin install exited ${inst.status}` };
  }
  return { ok: true, error: null };
}

export function writeCompanionConfig({ serverUrl, apiKey, env = process.env, home, fs = nodeFs }) {
  const path = env.ARCHIVIST_COMPANION_CONFIG || join(home, ".archivist-companion.json");
  let cur = {};
  if (fs.existsSync(path)) {
    try { cur = JSON.parse(fs.readFileSync(path, "utf8")); } catch { cur = {}; }
  }
  const next = { ...cur, serverUrl, apiKey };
  fs.writeFileSync(path, JSON.stringify(next, null, 2) + "\n");
  return { path };
}

export async function run({ argv, deps = {} }) {
  const log = deps.log || ((m) => process.stdout.write(m + "\n"));
  const home = deps.home || homedir();
  const cwd = deps.cwd || process.cwd();
  const env = deps.env || process.env;
  const _verify = deps.verify || verify;
  const _registerClaudeMcp = deps.registerClaudeMcp || registerClaudeMcp;
  const _writeAgentConfigs = deps.writeAgentConfigs || writeAgentConfigs;
  const _installCompanion = deps.installCompanion || installCompanion;
  const _writeCompanionConfig = deps.writeCompanionConfig || writeCompanionConfig;

  const args = parseArgs(argv);
  if (args.help) { log(USAGE); return 0; }

  if (!["user", "project", "local"].includes(args.scope)) {
    log(`error: --scope must be one of: user, project, local (got: ${args.scope})`);
    return 1;
  }

  let { server, key } = args;
  if ((!server || !key) && !args.yes) {
    // Minimal interactive fallback (skipped in tests, which always pass flags).
    const { createInterface } = await import("node:readline/promises");
    const rl = createInterface({ input: process.stdin, output: process.stdout });
    if (!server) server = (await rl.question("Archivist server URL: ")).trim();
    if (!key) key = (await rl.question("Team API key (arc_...): ")).trim();
    rl.close();
  }
  if (!server || !key) {
    log("error: --server and --key are required (or run without --yes to be prompted).");
    return 1;
  }

  const v = await _verify({ serverUrl: server, apiKey: key, cwd });
  if (!v.ok) {
    log(`error: could not verify ${server}: ${v.error}`);
    log("nothing was changed. check the URL and key, then re-run.");
    return 1;
  }
  log(`✓ connected${v.team ? ` (team: ${v.team})` : ""}`);

  const mcp = _registerClaudeMcp({ serverUrl: server, apiKey: key, scope: args.scope });
  if (mcp.ok) log(`✓ registered archivist MCP in Claude Code (scope: ${args.scope})`);
  else if (mcp.skipped) log("• claude CLI not found — skipped Claude Code MCP registration");
  else log(`• Claude Code MCP registration failed: ${mcp.error}`);

  try {
    const agents = _writeAgentConfigs({ serverUrl: server, apiKey: key, home });
    if (agents.codex) log("✓ added archivist to ~/.codex/config.toml (export ARCHIVIST_TOKEN to use it)");
    if (agents.cursor) log("✓ added archivist to ~/.cursor/mcp.json");
  } catch (err) {
    log(`• could not write agent configs: ${err && err.message ? err.message : String(err)}`);
  }

  if (args.companion) {
    const inst = _installCompanion({ scope: args.scope });
    if (inst.ok) log("✓ installed archivist-companion plugin");
    else log(`• companion plugin install skipped: ${inst.error}`);
    try {
      const cfg = _writeCompanionConfig({ serverUrl: server, apiKey: key, env, home });
      log(`✓ wrote companion config: ${cfg.path}`);
    } catch (err) {
      log(`• could not write companion config: ${err && err.message ? err.message : String(err)}`);
    }
  }

  log("\nDone. Verify with: claude mcp list");
  return 0;
}
