import { isConfigured } from "./config.js";
import { runHeadless } from "./headless.js";
import { PROTOCOL, buildMergePrompt } from "./protocol.js";

const DAY_MS = 86400000;

// Keep entries that are compaction_due AND older than the grace window — before
// that, an in-session reader is expected to compact for free.
export function selectDue(entries, nowMs, graceDays) {
  const graceMs = graceDays * DAY_MS;
  return (entries || []).filter(
    (e) => e.compaction_due && nowMs - Date.parse(e.updated_at) >= graceMs
  );
}

// C2 orchestration. Zero LLM calls when nothing is due.
export async function runSweep({ cfg, slug, client, headless = runHeadless, clock = Date.now }) {
  if (!isConfigured(cfg)) return { skipped: "unconfigured" };
  const list = await client.memoryList(slug);
  const due = selectDue(list.entries, clock(), cfg.sweepGraceDays);
  if (due.length === 0) return { compacted: 0, failed: 0 };
  // Isolate each keyword: one merge run rejecting (or reporting error) must not
  // abort the batch or blank the whole run. Always report failed so cron logs
  // it even when zero. Least-privilege tools: a merge only needs get + upsert.
  let compacted = 0;
  let failed = 0;
  for (const entry of due) {
    try {
      const r = await headless({
        cfg,
        prompt: buildMergePrompt(slug, entry.keyword, PROTOCOL),
        allowedTools: "mcp__archivist__memory_get,mcp__archivist__memory_upsert",
        model: cfg.sweepModel,
        maxTurns: cfg.sweepMaxTurns,
        detached: false,
      });
      if (r && r.error) failed++;
      else compacted++;
    } catch {
      failed++;
    }
  }
  return { compacted, failed };
}
