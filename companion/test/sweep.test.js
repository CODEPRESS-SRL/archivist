import { test } from "node:test";
import assert from "node:assert/strict";
import { selectDue, runSweep } from "../lib/sweep.js";

const DAY = 86400000;
const NOW = Date.parse("2026-07-10T00:00:00Z");

test("selectDue keeps only due AND older-than-grace entries", () => {
  const entries = [
    { keyword: "a", compaction_due: true, updated_at: "2026-07-01T00:00:00Z" },  // due, 9d old → keep
    { keyword: "b", compaction_due: true, updated_at: "2026-07-09T00:00:00Z" },  // due, 1d old → skip (grace)
    { keyword: "c", compaction_due: false, updated_at: "2026-01-01T00:00:00Z" }, // not due → skip
  ];
  const due = selectDue(entries, NOW, 3);
  assert.deepEqual(due.map((e) => e.keyword), ["a"]);
});

test("runSweep makes ZERO headless calls when nothing is due", async () => {
  let calls = 0;
  const client = { memoryList: async () => ({ entries: [{ keyword: "b", compaction_due: true, updated_at: "2026-07-09T00:00:00Z" }] }) };
  const headless = async () => { calls += 1; };
  const out = await runSweep({ cfg: { serverUrl: "u", apiKey: "k", sweepGraceDays: 3, sweepModel: "sonnet", sweepMaxTurns: 12 }, slug: "p", client, headless, clock: () => NOW });
  assert.deepEqual(out, { compacted: 0, failed: 0 });
  assert.equal(calls, 0);
});

test("runSweep spawns one non-detached merge per due keyword", async () => {
  const opts = [];
  const client = { memoryList: async () => ({ entries: [
    { keyword: "a", compaction_due: true, updated_at: "2026-07-01T00:00:00Z" },
    { keyword: "d", compaction_due: true, updated_at: "2026-06-01T00:00:00Z" },
  ] }) };
  const headless = async (o) => { opts.push(o); return { code: 0 }; };
  const out = await runSweep({ cfg: { serverUrl: "u", apiKey: "k", sweepGraceDays: 3, sweepModel: "sonnet", sweepMaxTurns: 12 }, slug: "billing-api", client, headless, clock: () => NOW });
  assert.deepEqual(out, { compacted: 2, failed: 0 });
  assert.equal(opts.length, 2);
  assert.notEqual(opts[0].detached, true);
  assert.match(opts[0].prompt, /billing-api/);
  assert.match(opts[0].prompt, /\ba\b/);
  assert.equal(opts[0].allowedTools, "mcp__archivist__memory_get,mcp__archivist__memory_upsert");
});

test("runSweep isolates a per-keyword failure and keeps sweeping the rest", async () => {
  const seen = [];
  const client = { memoryList: async () => ({ entries: [
    { keyword: "a", compaction_due: true, updated_at: "2026-07-01T00:00:00Z" },
    { keyword: "d", compaction_due: true, updated_at: "2026-06-01T00:00:00Z" },
  ] }) };
  const headless = async (o) => {
    seen.push(o.prompt);
    if (/keyword "a"/.test(o.prompt)) throw new Error("boom");
    return { code: 0 };
  };
  const out = await runSweep({ cfg: { serverUrl: "u", apiKey: "k", sweepGraceDays: 3, sweepModel: "sonnet", sweepMaxTurns: 12 }, slug: "billing-api", client, headless, clock: () => NOW });
  assert.deepEqual(out, { compacted: 1, failed: 1 });
  assert.equal(seen.length, 2); // the throw for "a" did NOT abort the batch
  assert.match(seen[1], /keyword "d"/);
});

test("runSweep skips when unconfigured", async () => {
  const out = await runSweep({ cfg: { serverUrl: null, apiKey: null }, slug: "p", client: { memoryList: async () => ({ entries: [] }) } });
  assert.deepEqual(out, { skipped: "unconfigured" });
});
