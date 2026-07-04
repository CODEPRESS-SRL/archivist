import { test } from "node:test";
import assert from "node:assert/strict";
import { slugify, deriveSlug } from "../lib/slug.js";

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
