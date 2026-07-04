import { test } from "node:test";
import assert from "node:assert/strict";
import { readTranscript } from "../lib/transcript.js";

const JSONL = [
  JSON.stringify({ type: "user", message: { content: "hello there" } }),
  JSON.stringify({ type: "assistant", message: { content: [{ type: "text", text: "hi back" }, { type: "tool_use", name: "Edit", input: {} }] } }),
  "not json — ignored",
].join("\n");

test("readTranscript extracts text, counts lines and file edits", () => {
  const t = readTranscript("/x", () => JSONL);
  assert.match(t.text, /hello there/);
  assert.match(t.text, /hi back/);
  assert.equal(t.fileEdits, 1);
  assert.ok(t.charCount > 0);
});

test("readTranscript is fail-silent on a missing file", () => {
  const t = readTranscript("/x", () => { throw new Error("ENOENT"); });
  assert.deepEqual(t, { lineCount: 0, charCount: 0, text: "", fileEdits: 0 });
});

test("readTranscript is fail-silent on a non-string read result", () => {
  assert.doesNotThrow(() => readTranscript("/x", () => 12345));
  assert.deepEqual(readTranscript("/x", () => 12345), { lineCount: 0, charCount: 0, text: "", fileEdits: 0 });
  assert.deepEqual(readTranscript("/x", () => undefined), { lineCount: 0, charCount: 0, text: "", fileEdits: 0 });
});

test("readTranscript reads a top-level content field", () => {
  const jsonl = JSON.stringify({ content: "top level text" });
  const t = readTranscript("/x", () => jsonl);
  assert.match(t.text, /top level text/);
});
