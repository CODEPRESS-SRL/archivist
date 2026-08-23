#!/usr/bin/env node
// C2 sweep entry for cron / Task Scheduler / manual `node scripts/sweep-cli.js`.
// Fail-silent; exit 0 always so a scheduler never sees a hard failure.
if (process.env.ARCHIVIST_HEADLESS === "1") process.exit(0);
// NOTE: keep all imported lib/*.js modules side-effect-free at top level — ESM evaluates imports before this guard runs.

import { loadConfig } from "../lib/config.js";
import { deriveSlug } from "../lib/slug.js";
import { createClient } from "../lib/mcp-client.js";
import { runSweep } from "../lib/sweep.js";
import { finishSilently } from "../lib/finish.js";
import { runStartupGc } from "../lib/gc.js";
import { log } from "../lib/log.js";

(async () => {
  // Reclaim leftovers from earlier sessions (including SIGKILLed ones)
  // before doing anything else. Never throws, never blocks startup.
  const gc = runStartupGc();
  if (gc.temp.removed || gc.cache.removed) {
    log(`gc: removed ${gc.temp.removed} temp dir(s), ${gc.cache.removed} cache entr(y|ies)`);
  }
  try {
    const cwd = process.argv[2] || process.cwd();
    const cfg = loadConfig();
    const slug = deriveSlug(cwd);
    const client = createClient({ serverUrl: cfg.serverUrl, apiKey: cfg.apiKey });
    const result = await runSweep({ cfg, slug, client });
    process.stdout.write(JSON.stringify({ slug, ...result }) + "\n");
  } catch (err) {
    // fail-silent. Visible only with ARCHIVIST_DEBUG=1.
    log(`sweep: ${err && err.message ? err.message : err}`);
  }
  // Drain instead of forcing exit: the mcp-client's undici socket is still
  // closing here, and process.exit(0) would race it into a libuv crash on
  // Windows (see lib/finish.js).
  finishSilently();
})();
