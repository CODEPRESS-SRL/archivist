import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

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

// Load companion config from ~/.archivist-companion.json (or ARCHIVIST_COMPANION_CONFIG),
// then apply env overrides. Never throws: a missing/malformed file yields defaults.
export function loadConfig(env = process.env, home = homedir()) {
  const path = env.ARCHIVIST_COMPANION_CONFIG || join(home, ".archivist-companion.json");
  let fileCfg = {};
  try {
    fileCfg = JSON.parse(readFileSync(path, "utf8"));
  } catch {
    fileCfg = {};
  }
  const cfg = { ...DEFAULTS, ...fileCfg };
  if (env.ARCHIVIST_SERVER_URL) cfg.serverUrl = env.ARCHIVIST_SERVER_URL;
  if (env.ARCHIVIST_API_KEY) cfg.apiKey = env.ARCHIVIST_API_KEY;
  return cfg;
}

export function isConfigured(cfg) {
  return Boolean(cfg.serverUrl && cfg.apiKey);
}
