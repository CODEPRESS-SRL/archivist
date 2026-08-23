import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { resolvePaths } from "./paths.js";
import { log } from "./log.js";

const DEFAULTS = {
  serverUrl: null,
  apiKey: null,
  capture: true,
  index: true,
  captureModel: "haiku",
  captureMaxTurns: 12,
  gateMinChars: 2000,
  sweepGraceDays: 3,
  sweepModel: "sonnet",
  sweepMaxTurns: 12,
};

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

// Load companion config from the Archivist root (see paths.js), falling back to
// the pre-1.1 `~/.archivist-companion.json` so existing installs keep working;
// the next write migrates it. Env overrides last. Never throws: a
// missing/malformed file yields defaults.
export function loadConfig(env = process.env, home = homedir()) {
  if (env.ARCHIVIST_COMPANION_CONFIG) {
    log(
      "ARCHIVIST_COMPANION_CONFIG is no longer supported; config is read from " +
        "the Archivist root. Use ARCHIVIST_HOME to relocate it.",
      { env }
    );
  }
  const paths = resolvePaths({ env, home });
  const fileCfg = readJson(paths.configFile) || readJson(paths.legacyConfigFile) || {};
  const cfg = { ...DEFAULTS, ...fileCfg };
  if (env.ARCHIVIST_SERVER_URL) cfg.serverUrl = env.ARCHIVIST_SERVER_URL;
  if (env.ARCHIVIST_API_KEY) cfg.apiKey = env.ARCHIVIST_API_KEY;
  return cfg;
}

export function isConfigured(cfg) {
  return Boolean(cfg.serverUrl && cfg.apiKey);
}
