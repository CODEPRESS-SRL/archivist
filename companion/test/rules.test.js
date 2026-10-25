import { test } from "node:test";
import assert from "node:assert/strict";
import { RULE, BEGIN, END, upsertManagedBlock, writeAgentRules } from "../lib/rules.js";

// in-memory fs matching the connect.test.js memFs contract; a directory is a
// key whose presence makes existsSync true.
function memFs(seed = {}) {
  const files = { ...seed };
  return {
    files,
    existsSync: (p) => Object.prototype.hasOwnProperty.call(files, p),
    readFileSync: (p) => files[p],
    writeFileSync: (p, data) => { files[p] = data; },
  };
}

test("RULE is self-delimiting with begin/end markers", () => {
  assert.ok(RULE.includes(BEGIN));
  assert.ok(RULE.includes(END));
  assert.match(RULE, /Archivist shared memory/);
  assert.match(RULE, /memory_search/);
});

test("upsertManagedBlock: appends block when markers absent", () => {
  const out = upsertManagedBlock("# My rules\n\n- do stuff\n");
  assert.match(out, /# My rules/);
  assert.ok(out.includes(RULE));
  // original content preserved before the block
  assert.ok(out.indexOf("do stuff") < out.indexOf(BEGIN));
});

test("upsertManagedBlock: appends cleanly to empty content", () => {
  const out = upsertManagedBlock("");
  assert.equal(out.trimEnd(), RULE.trimEnd());
});

test("upsertManagedBlock: replaces existing block in place, preserving surroundings", () => {
  const withBlock = upsertManagedBlock("# Top\n");
  const stale = withBlock.replace("Archivist shared memory", "OLD HEADING");
  const fixed = upsertManagedBlock(stale);
  assert.match(fixed, /# Top/);
  assert.ok(fixed.includes(RULE));
  assert.ok(!fixed.includes("OLD HEADING"));
  // exactly one block
  assert.equal(fixed.match(new RegExp(BEGIN.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g")).length, 1);
});

test("upsertManagedBlock: idempotent (running twice is a fixed point)", () => {
  const once = upsertManagedBlock("# Top\n");
  const twice = upsertManagedBlock(once);
  assert.equal(twice, once);
});

test("writeAgentRules: creates CLAUDE.md when ~/.claude exists but file absent", () => {
  const fs = memFs({ "/home/u/.claude": "" }); // dir present
  const r = writeAgentRules({ home: "/home/u", fs });
  assert.equal(r.claude, true);
  assert.ok(fs.files["/home/u/.claude/CLAUDE.md"].includes(RULE));
});

test("writeAgentRules: appends to existing CLAUDE.md, preserves prior content", () => {
  const fs = memFs({ "/home/u/.claude": "", "/home/u/.claude/CLAUDE.md": "# Mine\n\n- rule one\n" });
  const r = writeAgentRules({ home: "/home/u", fs });
  assert.equal(r.claude, true);
  const out = fs.files["/home/u/.claude/CLAUDE.md"];
  assert.match(out, /# Mine/);
  assert.match(out, /rule one/);
  assert.ok(out.includes(RULE));
});

test("writeAgentRules: writes AGENTS.md when ~/.codex exists", () => {
  const fs = memFs({ "/home/u/.codex": "" });
  const r = writeAgentRules({ home: "/home/u", fs });
  assert.equal(r.codex, true);
  assert.ok(fs.files["/home/u/.codex/AGENTS.md"].includes(RULE));
});

test("writeAgentRules: no agent dirs → both false, no writes", () => {
  const fs = memFs();
  const r = writeAgentRules({ home: "/home/u", fs });
  assert.deepEqual(r, { claude: false, codex: false });
  assert.deepEqual(Object.keys(fs.files), []);
});

test("writeAgentRules: re-run over already-configured file is a no-op (returns false)", () => {
  const fs = memFs({ "/home/u/.claude": "" });
  writeAgentRules({ home: "/home/u", fs });
  const r2 = writeAgentRules({ home: "/home/u", fs });
  assert.equal(r2.claude, false);
  assert.equal(fs.files["/home/u/.claude/CLAUDE.md"].match(new RegExp(BEGIN.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g")).length, 1);
});
