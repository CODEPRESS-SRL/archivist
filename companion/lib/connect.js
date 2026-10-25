import { spawnSync } from "node:child_process";
import * as nodeFs from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path/posix";
import { fileURLToPath } from "node:url";
import { dirname } from "node:path";
import { createClient as realCreateClient } from "./mcp-client.js";
import { deriveSlug as realDeriveSlug } from "./slug.js";
import { writeAgentRules as realWriteAgentRules } from "./rules.js";
import { resolvePaths } from "./paths.js";
import { runStartupGc } from "./gc.js";
import { pathOnlyEnv, resolveFromPathOnly } from "./exe-path.js";

export const USAGE = `archivist-connect — connect this machine's coding agent to an Archivist server.

Usage:
  npx archivist-connect --server <url> --key <arc_...> [options]

Options:
  --server <url>        Archivist MCP endpoint (e.g. https://host/mcp/)
  --key <arc_...>       Team API key
  --scope <s>           Claude Code scope: user | project | local (default: user)
  --no-companion        Register MCP only; skip companion install + config
  --no-rules            Skip writing the Archivist rule into agent rules files
  --yes                 Non-interactive; fail instead of prompting for missing values
  -h, --help            Show this help
`;

export function parseArgs(argv) {
  const out = { server: null, key: null, scope: "user", companion: true, rules: true, yes: false, help: false };
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
      case "--no-rules": out.rules = false; break;
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

const NOT_FOUND = Object.freeze({ notFound: true, status: null, stdout: "", stderr: "" });
const CMD_UNSAFE_CHARACTERS = /[%&|^<>"!\r\n]/;
const CMD_NOT_RECOGNIZED = /['"]claude\.cmd['"]/i;

const UNSAFE_REGISTER_ERROR =
  "server URL or key contains characters that cannot be passed safely to claude.cmd; register manually: " +
  'claude mcp add --transport http archivist <url> -H "Authorization: Bearer <key>" -s user';
const UNSAFE_INSTALL_ERROR =
  "package path contains characters that cannot be passed safely to claude.cmd; install manually: " +
  "claude plugin marketplace add <package dir>, then claude plugin install archivist-companion@archivist-tools";

const isEnoent = (result) => Boolean(result.error && result.error.code === "ENOENT");
const toRunResult = (result) => ({
  notFound: false, status: result.status, stdout: result.stdout || "", stderr: result.stderr || "",
});
const quoteForCmd = (arg) => `"${arg.replace(/(\\+)$/, "$1$1")}"`;
const firstLine = (text) => (text || "").split(/\r?\n/, 1)[0];

/**
 * Runs the `claude` CLI and returns `{ notFound, status, stdout, stderr }`.
 *
 * The first attempt is a shell-less spawn of `claude` (resolves `claude.exe`
 * or a `claude` binary on PATH). Only on win32, when that spawn fails with
 * ENOENT, it retries the npm `.cmd` shim through cmd.exe as one command
 * string: `claude.cmd "arg1" "arg2" ...`. Before that retry, any arg holding
 * a character cmd.exe could interpret (`% & | ^ < > " !`, CR, LF) is refused:
 * nothing is spawned and the result carries `unsafe: true`. Trailing
 * backslashes are doubled so they cannot escape the closing quote. Both
 * spawns set `NoDefaultCurrentDirectoryInExePath` in this process and in the
 * child, so neither Node nor cmd.exe resolves `claude` or `claude.cmd` from
 * the current directory; the cwd itself is never changed. A missing
 * `claude.cmd` (exit 1 with the quoted name on the first stderr line, in any
 * locale) reports `notFound: true`. Off win32 no shell is ever used.
 *
 * @param {string[]} args CLI arguments for `claude`.
 * @param {{ platform?: string, spawn?: typeof spawnSync }} [options] Injection points for tests.
 */
export function defaultRunClaude(args, { platform = process.platform, spawn = spawnSync } = {}) {
  const direct = resolveFromPathOnly(() => spawn("claude", args, { encoding: "utf8", env: pathOnlyEnv() }));
  if (!isEnoent(direct)) return toRunResult(direct);
  if (platform !== "win32") return { ...NOT_FOUND };
  if (args.some((arg) => CMD_UNSAFE_CHARACTERS.test(arg))) {
    return { notFound: false, unsafe: true, status: null, stdout: "", stderr: "" };
  }

  const commandString = ["claude.cmd", ...args.map(quoteForCmd)].join(" ");
  const viaShell = resolveFromPathOnly(() => spawn(commandString, {
    encoding: "utf8",
    shell: true,
    env: pathOnlyEnv(),
  }));
  if (isEnoent(viaShell) || (viaShell.status === 1 && CMD_NOT_RECOGNIZED.test(firstLine(viaShell.stderr)))) {
    return { ...NOT_FOUND };
  }
  return toRunResult(viaShell);
}

const PROJECT_TOKEN_REFERENCE = "${ARCHIVIST_TOKEN}";

/**
 * Registers the archivist MCP server with `claude mcp add` at `scope`,
 * removing any previous registration first. Scope `project` writes the
 * header into the project's `.mcp.json`, which is meant to be committed, so
 * it registers the literal `${ARCHIVIST_TOKEN}` reference that Claude Code
 * expands from the environment instead of the key; `user` and `local` keep
 * the key itself.
 *
 * @returns {{ ok: boolean, skipped: boolean, error: string | null }}
 */
export function registerClaudeMcp({ serverUrl, apiKey, scope, runClaude = defaultRunClaude }) {
  // Idempotent: remove any existing registration first (ignore its result), then add.
  const probe = runClaude(["mcp", "remove", "archivist", "-s", scope]);
  if (probe.notFound) return { ok: false, skipped: true, error: null };

  const bearer = scope === "project" ? PROJECT_TOKEN_REFERENCE : apiKey;
  const add = runClaude([
    "mcp", "add", "--transport", "http", "archivist", serverUrl,
    "-H", `Authorization: Bearer ${bearer}`, "-s", scope,
  ]);
  if (add.notFound) return { ok: false, skipped: true, error: null };
  if (add.unsafe) return { ok: false, skipped: false, error: UNSAFE_REGISTER_ERROR };
  if (add.status !== 0) {
    return { ok: false, skipped: false, error: add.stderr || `claude mcp add exited ${add.status}` };
  }
  return { ok: true, skipped: false, error: null };
}

/**
 * Adds the archivist MCP server to Codex and Cursor, auto-detected by their
 * home directories (`~/.codex`, `~/.cursor`). An agent whose directory is
 * absent is left alone; when the directory exists but its config file does
 * not, the file is created. Codex gets a `[mcp_servers.archivist]` block that
 * reads the key from `ARCHIVIST_TOKEN` (appended once, never duplicated).
 * Cursor's `mcp.json` is merged; when it is not a valid JSON object it is left
 * untouched and `cursorInvalid` is true. `mcp.json` holds the Bearer key, so it
 * is made owner-only (0600) after every write, not only on creation. Never
 * installs the companion for them.
 *
 * @returns {{ codex: boolean, cursor: boolean, cursorInvalid: boolean }} which files were written.
 */
export function writeAgentConfigs({ serverUrl, apiKey, home, fs = nodeFs }) {
  const result = { codex: false, cursor: false, cursorInvalid: false };

  const codexPath = join(home, ".codex", "config.toml");
  const codexExists = fs.existsSync(codexPath);
  if (codexExists || fs.existsSync(join(home, ".codex"))) {
    const cur = codexExists ? fs.readFileSync(codexPath, "utf8") : "";
    if (!cur.includes("[mcp_servers.archivist]")) {
      const block =
        `[mcp_servers.archivist]\n` +
        `url = ${JSON.stringify(serverUrl)}\n` +
        `bearer_token_env_var = "ARCHIVIST_TOKEN"\n`;
      const separator = cur === "" ? "" : cur.endsWith("\n") ? "\n" : "\n\n";
      fs.writeFileSync(codexPath, cur + separator + block);
      result.codex = true;
    }
  }

  const cursorPath = join(home, ".cursor", "mcp.json");
  const cursorExists = fs.existsSync(cursorPath);
  if (cursorExists || fs.existsSync(join(home, ".cursor"))) {
    let json = {};
    if (cursorExists) {
      try { json = JSON.parse(fs.readFileSync(cursorPath, "utf8")); } catch { json = null; }
    }
    if (!json || typeof json !== "object" || Array.isArray(json)) {
      result.cursorInvalid = true;
      return result;
    }
    if (!json.mcpServers) json.mcpServers = {};
    json.mcpServers.archivist = { url: serverUrl, headers: { Authorization: `Bearer ${apiKey}` } };
    fs.writeFileSync(cursorPath, JSON.stringify(json, null, 2) + "\n", cursorExists ? undefined : { mode: 0o600 });
    try { fs.chmodSync(cursorPath, 0o600); } catch {}
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
  if (mkt.unsafe) return { ok: false, error: UNSAFE_INSTALL_ERROR };
  // status !== 0 is tolerated here (marketplace may already be registered).

  const inst = runClaude(["plugin", "install", "archivist-companion@archivist-tools", "-s", scope]);
  if (inst.notFound) return { ok: false, error: "claude CLI not found" };
  if (inst.unsafe) return { ok: false, error: UNSAFE_INSTALL_ERROR };
  if (inst.status !== 0) {
    return { ok: false, error: inst.stderr || `claude plugin install exited ${inst.status}` };
  }
  return { ok: true, error: null };
}

// Writes into the Archivist root (creating it). Settings from a pre-1.1
// `~/.archivist-companion.json` are carried over and that file is removed, so
// upgrading leaves nothing behind in the root of the home directory.
export function writeCompanionConfig({ serverUrl, apiKey, env = process.env, home, fs = nodeFs }) {
  const paths = resolvePaths({ env, home });
  const read = (p) => {
    try { return JSON.parse(fs.readFileSync(p, "utf8")); } catch { return null; }
  };
  let cur = {};
  let migrated = false;
  if (fs.existsSync(paths.configFile)) {
    cur = read(paths.configFile) || {};
  } else if (fs.existsSync(paths.legacyConfigFile)) {
    cur = read(paths.legacyConfigFile) || {};
    migrated = true;
  }
  const next = { ...cur, serverUrl, apiKey };
  fs.mkdirSync(paths.data, { recursive: true });
  fs.writeFileSync(paths.configFile, JSON.stringify(next, null, 2) + "\n", { mode: 0o600 });
  // The file holds a team API key: keep it owner-only even when it pre-existed
  // with laxer permissions (no-op on Windows).
  try { fs.chmodSync(paths.configFile, 0o600); } catch { /* not supported here */ }
  if (migrated) {
    try { fs.rmSync(paths.legacyConfigFile, { force: true }); } catch { /* leave it */ }
  }
  return { path: paths.configFile, migrated };
}

const SERVER_URL_FORBIDDEN = /[\p{Cc}\s"'`]/u;
const SERVER_URL_EXAMPLE = "https://archivist.example.com/mcp/";

/**
 * Checks the raw `--server` value before anything uses it: it must parse with
 * `new URL()`, use http: or https:, and hold no control character, quote or
 * whitespace.
 *
 * @param {string} raw The value as typed.
 * @returns {string | null} An actionable error message, or null when valid.
 */
export function validateServerUrl(raw) {
  if (SERVER_URL_FORBIDDEN.test(raw)) {
    return `--server must not contain spaces, quotes or control characters (got: ${JSON.stringify(raw)}); ` +
      `pass the bare endpoint, e.g. ${SERVER_URL_EXAMPLE}`;
  }
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    return `--server is not a valid URL (got: ${JSON.stringify(raw)}); pass the full endpoint, e.g. ${SERVER_URL_EXAMPLE}`;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return `--server must use http:// or https:// (got: ${parsed.protocol}); pass e.g. ${SERVER_URL_EXAMPLE}`;
  }
  return null;
}

export async function run({ argv, deps = {} }) {
  const log = deps.log || ((m) => process.stdout.write(m + "\n"));
  const home = deps.home || homedir();
  const cwd = deps.cwd || process.cwd();
  const env = deps.env || process.env;
  const platform = deps.platform || process.platform;
  const _verify = deps.verify || verify;
  const _registerClaudeMcp = deps.registerClaudeMcp || registerClaudeMcp;
  const _writeAgentConfigs = deps.writeAgentConfigs || writeAgentConfigs;
  const _writeAgentRules = deps.writeAgentRules || realWriteAgentRules;
  const _installCompanion = deps.installCompanion || installCompanion;
  const _writeCompanionConfig = deps.writeCompanionConfig || writeCompanionConfig;

  const args = parseArgs(argv);
  if (args.help) { log(USAGE); return 0; }

  // Sweep whatever a previous, abruptly-killed run left behind. Never blocks.
  runStartupGc({ paths: resolvePaths({ env, home }) });

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
  const serverError = validateServerUrl(server);
  if (serverError) {
    log(`error: ${serverError}`);
    return 1;
  }

  const v = await _verify({ serverUrl: server, apiKey: key, cwd });
  if (!v.ok) {
    log(`error: could not verify ${server}: ${v.error}`);
    log("nothing was changed. check the URL and key, then re-run.");
    return 1;
  }
  log(`✓ connected${v.team ? ` (team: ${v.team})` : ""}`);

  const tokenHint = platform === "win32"
    ? 'setx ARCHIVIST_TOKEN "arc_xxx"'
    : "export ARCHIVIST_TOKEN=arc_xxx";

  const mcp = _registerClaudeMcp({ serverUrl: server, apiKey: key, scope: args.scope });
  if (mcp.ok) {
    log(`✓ registered archivist MCP in Claude Code (scope: ${args.scope})`);
    if (args.scope === "project") {
      log("  .mcp.json holds ${ARCHIVIST_TOKEN}, not the key; Claude Code reads it from the environment.");
      log(`  every teammate must set ARCHIVIST_TOKEN before starting Claude Code: ${tokenHint}`);
    }
  } else if (mcp.skipped) {
    log("• claude CLI not found — skipped Claude Code MCP registration");
  } else {
    log(`• Claude Code MCP registration failed: ${mcp.error}`);
  }

  try {
    const agents = _writeAgentConfigs({ serverUrl: server, apiKey: key, home });
    if (agents.codex) {
      log("✓ added archivist to ~/.codex/config.toml");
      log(`  Codex reads the key from ARCHIVIST_TOKEN; set it with: ${tokenHint}`);
    }
    if (agents.cursor) log("✓ added archivist to ~/.cursor/mcp.json");
    if (agents.cursorInvalid) {
      log("• ~/.cursor/mcp.json is not valid JSON — left untouched; add the archivist server to it manually");
    }
  } catch (err) {
    log(`• could not write agent configs: ${err && err.message ? err.message : String(err)}`);
  }

  if (args.rules) {
    try {
      const rules = _writeAgentRules({ home });
      if (rules.claude) log("✓ added Archivist rule to ~/.claude/CLAUDE.md");
      if (rules.codex) log("✓ added Archivist rule to ~/.codex/AGENTS.md");
    } catch (err) {
      log(`• could not write agent rules: ${err && err.message ? err.message : String(err)}`);
    }
  }

  if (args.companion) {
    const inst = _installCompanion({ scope: args.scope });
    if (inst.ok) log("✓ installed archivist-companion plugin");
    else log(`• companion plugin install skipped: ${inst.error}`);
    try {
      const cfg = _writeCompanionConfig({ serverUrl: server, apiKey: key, env, home });
      log(`✓ wrote companion config: ${cfg.path}`);
      if (cfg.migrated) log("  (migrated from ~/.archivist-companion.json, now removed)");
    } catch (err) {
      log(`• could not write companion config: ${err && err.message ? err.message : String(err)}`);
    }
  }

  log("\nDone. Verify with: claude mcp list");
  return 0;
}
