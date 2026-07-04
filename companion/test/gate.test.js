import { test } from "node:test";
import assert from "node:assert/strict";
import { shouldCapture } from "../lib/gate.js";

test("captures a long session", () => {
  assert.equal(shouldCapture({ transcript: { charCount: 5000, fileEdits: 0 }, minChars: 2000 }), true);
});

test("captures a short session that changed files", () => {
  assert.equal(shouldCapture({ transcript: { charCount: 400, fileEdits: 2 }, minChars: 2000 }), true);
});

test("skips a trivial short session with no edits", () => {
  assert.equal(shouldCapture({ transcript: { charCount: 300, fileEdits: 0 }, minChars: 2000 }), false);
});

test("skips an empty transcript even with a spurious edit count", () => {
  assert.equal(shouldCapture({ transcript: { charCount: 0, fileEdits: 3 }, minChars: 2000 }), false);
});
