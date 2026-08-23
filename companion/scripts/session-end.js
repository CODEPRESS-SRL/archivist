#!/usr/bin/env node
// C1 auto-capture entry. Fail-silent: any error → exit 0; never blocks teardown.
if (process.env.ARCHIVIST_HEADLESS === "1") process.exit(0);
// NOTE: keep all imported lib/*.js modules side-effect-free at top level — ESM evaluates imports before this guard runs.

import { loadConfig } from "../lib/config.js";
import { deriveSlug } from "../lib/slug.js";
import { readTranscript } from "../lib/transcript.js";
import { runCapture } from "../lib/capture.js";
import { runStartupGc } from "../lib/gc.js";
import { log } from "../lib/log.js";

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
  // Reclaim leftovers from earlier sessions (including SIGKILLed ones)
  // before doing anything else. Never throws, never blocks startup.
  const gc = runStartupGc();
  if (gc.temp.removed || gc.cache.removed) {
    log(`gc: removed ${gc.temp.removed} temp dir(s), ${gc.cache.removed} cache entr(y|ies)`);
  }
  try {
    const input = JSON.parse((await readStdin()) || "{}");
    const cwd = input.cwd || process.cwd();
    const cfg = loadConfig();
    const slug = deriveSlug(cwd);
    const transcript = readTranscript(input.transcript_path || "");
    await runCapture({ cfg, slug, transcript });
  } catch (err) {
    // fail-silent. Visible only with ARCHIVIST_DEBUG=1.
    log(`session-end: ${err && err.message ? err.message : err}`);
  }
  process.exit(0);
})();
