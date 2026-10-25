import { test } from "node:test";
import assert from "node:assert/strict";
import { formatIndex, buildStartOutput } from "../lib/index-format.js";

test("formatIndex lists keywords and flags due ones", () => {
  const s = formatIndex([
    { keyword: "redis-cache", compaction_due: false },
    { keyword: "auth-flow", compaction_due: true },
  ]);
  assert.match(s, /redis-cache/);
  assert.match(s, /auth-flow ⚠ compaction due/);
});

test("formatIndex truncates to the char budget with an overflow line", () => {
  const many = Array.from({ length: 200 }, (_, i) => ({ keyword: `keyword-number-${i}`, compaction_due: false }));
  const s = formatIndex(many, 300);
  assert.ok(s.length <= 400); // budget + the overflow line slack
  assert.match(s, /and \d+ more/);
});

test("formatIndex returns empty string for no entries", () => {
  assert.equal(formatIndex([]), "");
});

test("buildStartOutput wraps the index in the SessionStart JSON, or null when empty", () => {
  const out = buildStartOutput("billing-api", [{ keyword: "k", compaction_due: false }]);
  const parsed = JSON.parse(out);
  assert.equal(parsed.hookSpecificOutput.hookEventName, "SessionStart");
  assert.match(parsed.hookSpecificOutput.additionalContext, /k/);
  assert.equal(buildStartOutput("p", []), null);
});
