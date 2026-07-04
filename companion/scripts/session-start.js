#!/usr/bin/env node
// C3 session-index entry. Fail-silent: any error → exit 0, inject nothing.
if (process.env.ARCHIVIST_HEADLESS === "1") process.exit(0);
// NOTE: keep all imported lib/*.js modules side-effect-free at top level — ESM evaluates imports before this guard runs.

import { loadConfig, isConfigured } from "../lib/config.js";
import { deriveSlug } from "../lib/slug.js";
import { createClient } from "../lib/mcp-client.js";
import { buildStartOutput } from "../lib/index-format.js";

function readStdin() {
  return new Promise((resolve) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (c) => (data += c));
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", () => resolve(""));
  });
}

(async () => {
  try {
    const input = JSON.parse((await readStdin()) || "{}");
    if (input.source === "resume") process.exit(0); // don't re-inject on resume
    const cfg = loadConfig();
    if (!isConfigured(cfg) || cfg.index === false) process.exit(0);
    const slug = deriveSlug(input.cwd || process.cwd());
    const client = createClient({ serverUrl: cfg.serverUrl, apiKey: cfg.apiKey });
    const list = await client.memoryList(slug);
    const out = buildStartOutput(slug, list.entries || []);
    if (out) process.stdout.write(out);
  } catch {
    // fail-silent — inject nothing
  }
  process.exit(0);
})();
