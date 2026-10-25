import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const packageRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

function packedPaths() {
  const result = process.platform === "win32"
    ? spawnSync("npm.cmd pack --dry-run --json", { cwd: packageRoot, encoding: "utf8", shell: true })
    : spawnSync("npm", ["pack", "--dry-run", "--json"], { cwd: packageRoot, encoding: "utf8" });
  assert.equal(result.status, 0, `npm pack failed: ${result.stderr}`);
  const [manifest] = JSON.parse(result.stdout);
  return manifest.files.map((file) => file.path.replaceAll("\\", "/"));
}

test("npm package ships the plugin, the CLIs and the docs, and nothing else", () => {
  const paths = packedPaths();
  for (const required of [
    ".claude-plugin/marketplace.json",
    ".claude-plugin/plugin.json",
    "hooks/hooks.json",
    "commands/sweep.md",
    "lib/connect.js",
    "scripts/connect.js",
    "scripts/archivist.js",
    "LICENSE",
    "README.md",
  ]) {
    assert.ok(paths.includes(required), `${required} missing from the package`);
  }
  assert.deepEqual(paths.filter((path) => path.startsWith("test/")), []);
  assert.ok(!paths.includes(".archivist-companion.example.json"));
});
