import { test } from "node:test";
import assert from "node:assert/strict";
import { buildCapturePrompt, runCapture } from "../lib/capture.js";

const CFG = { serverUrl: "https://x/mcp/", apiKey: "arc_k", capture: true, captureModel: "haiku", captureMaxTurns: 12, gateMinChars: 2000 };

test("prompt embeds slug, protocol, the <=3 cap and the exit-without-writing rule", () => {
  const p = buildCapturePrompt("billing-api", "session text", "PROTO-TEXT");
  assert.match(p, /billing-api/);
  assert.match(p, /PROTO-TEXT/);
  assert.match(p, /3 concepts/i);
  assert.match(p, /exit without writing/i);
});

test("runCapture skips when disabled", async () => {
  const out = await runCapture({ cfg: { ...CFG, capture: false }, slug: "p", transcript: { text: "x", charCount: 9999, fileEdits: 1 } });
  assert.deepEqual(out, { skipped: "disabled" });
});

test("runCapture skips a trivial session (gate)", async () => {
  let spawned = false;
  const headless = async () => { spawned = true; };
  const out = await runCapture({ cfg: CFG, slug: "p", transcript: { text: "x", charCount: 100, fileEdits: 0 }, headless });
  assert.deepEqual(out, { skipped: "gate" });
  assert.equal(spawned, false);
});

test("runCapture starts a detached headless run for a substantive session", async () => {
  let opts;
  const headless = async (o) => { opts = o; return { detached: true }; };
  const out = await runCapture({ cfg: CFG, slug: "billing-api", transcript: { text: "lots of work", charCount: 5000, fileEdits: 3 }, headless });
  assert.deepEqual(out, { started: true });
  assert.equal(opts.detached, true);
  assert.equal(opts.model, "haiku");
  assert.equal(opts.allowedTools, "mcp__archivist");
  assert.match(opts.prompt, /billing-api/);
});
