import { test } from "node:test";
import assert from "node:assert/strict";
import { slugify, deriveSlug, defaultExec } from "../lib/slug.js";

test("defaultExec resolves git from PATH only and keeps the cwd", () => {
  const guard = "NoDefaultCurrentDirectoryInExePath";
  const previous = process.env[guard];
  delete process.env[guard];
  try {
    let seen;
    const execFile = (cmd, args, opts) => {
      seen = { cmd, args, parentGuard: process.env[guard], opts };
      return "out";
    };
    assert.equal(defaultExec("git", ["-C", "/repo", "remote"], execFile), "out");
    assert.equal(seen.cmd, "git");
    assert.deepEqual(seen.args, ["-C", "/repo", "remote"]);
    assert.equal(seen.parentGuard, "1");
    assert.deepEqual(seen.opts.env, { ...process.env, [guard]: "1" });
    assert.equal("cwd" in seen.opts, false);
    assert.equal(guard in process.env, false);
  } finally {
    if (previous !== undefined) process.env[guard] = previous;
  }
});

test("slugify normalizes case, .git, and separators", () => {
  assert.equal(slugify("Billing-API.git"), "billing-api");
  assert.equal(slugify("Acme_Widgets"), "acme-widgets");
  assert.equal(slugify("  Spaced Name  "), "spaced-name");
});

test("deriveSlug from ssh remote", () => {
  const exec = () => "git@github.com:acme/Billing-API.git\n";
  assert.equal(deriveSlug("/repo", exec), "billing-api");
});

test("deriveSlug from https remote", () => {
  const exec = () => "https://github.com/acme/Billing-API.git\n";
  assert.equal(deriveSlug("/repo", exec), "billing-api");
});

test("deriveSlug falls back to directory basename when no remote", () => {
  const exec = () => { throw new Error("no origin"); };
  assert.equal(deriveSlug("/home/user/My_Project", exec), "my-project");
});
