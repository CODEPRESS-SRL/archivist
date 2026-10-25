import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseMcpBody, createClient } from "../lib/mcp-client.js";

test("parseMcpBody reads a plain JSON body", () => {
  const obj = parseMcpBody("application/json", '{"jsonrpc":"2.0","id":1,"result":{"ok":true}}');
  assert.deepEqual(obj.result, { ok: true });
});

test("parseMcpBody reads the data line of an SSE body", () => {
  const sse = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"n\":5}}\n\n";
  const obj = parseMcpBody("text/event-stream", sse);
  assert.deepEqual(obj.result, { n: 5 });
});

test("memoryList runs initialize then tools/call and returns structured payload", async () => {
  const calls = [];
  const fakeFetch = async (url, opts) => {
    const body = JSON.parse(opts.body);
    calls.push(body.method);
    let result;
    if (body.method === "initialize") {
      result = { protocolVersion: "2024-11-05", serverInfo: { name: "archivist" }, capabilities: {} };
    } else if (body.method === "notifications/initialized") {
      return { ok: true, status: 202, headers: new Map(), text: async () => "" };
    } else if (body.method === "tools/call") {
      assert.equal(body.params.name, "memory_list");
      assert.deepEqual(body.params.arguments, { project: "billing-api" });
      result = { structuredContent: { project: "billing-api", entries: [{ keyword: "redis-cache", compaction_due: false, updated_at: "2026-07-01T00:00:00Z" }] } };
    }
    return {
      ok: true,
      status: 200,
      headers: new Map([["content-type", "application/json"], ["mcp-session-id", "s1"]]),
      text: async () => JSON.stringify({ jsonrpc: "2.0", id: body.id ?? null, result }),
    };
  };
  const client = createClient({ serverUrl: "https://x/mcp/", apiKey: "arc_k", fetch: fakeFetch });
  const out = await client.memoryList("billing-api");
  assert.deepEqual(out.entries[0].keyword, "redis-cache");
  assert.deepEqual(calls, ["initialize", "notifications/initialized", "tools/call"]);
});

test("callTool falls back to JSON.parse(content[0].text) when no structuredContent", async () => {
  const fakeFetch = async (url, opts) => {
    const body = JSON.parse(opts.body);
    let result;
    if (body.method === "initialize") {
      result = { protocolVersion: "2024-11-05", capabilities: {} };
    } else if (body.method === "notifications/initialized") {
      return { ok: true, status: 202, headers: new Map(), text: async () => "" };
    } else if (body.method === "tools/call") {
      result = {
        content: [{ type: "text", text: JSON.stringify({ project: "p", entries: [{ keyword: "x" }] }) }],
      };
    }
    return {
      ok: true,
      status: 200,
      headers: new Map([["content-type", "application/json"]]),
      text: async () => JSON.stringify({ jsonrpc: "2.0", id: body.id ?? null, result }),
    };
  };
  const client = createClient({ serverUrl: "https://x/mcp/", apiKey: "arc_k", fetch: fakeFetch });
  const out = await client.memoryList("p");
  assert.equal(out.entries[0].keyword, "x");
});

test("callTool rejects on a JSON-RPC error", async () => {
  const fakeFetch = async (url, opts) => {
    const body = JSON.parse(opts.body);
    const result = body.method === "initialize" ? { capabilities: {} } : undefined;
    const payload = body.method === "tools/call"
      ? { jsonrpc: "2.0", id: body.id, error: { code: -32000, message: "boom" } }
      : { jsonrpc: "2.0", id: body.id ?? null, result };
    return { ok: true, status: 200, headers: new Map([["content-type", "application/json"]]), text: async () => JSON.stringify(payload) };
  };
  const client = createClient({ serverUrl: "https://x/mcp/", apiKey: "arc_k", fetch: fakeFetch });
  await assert.rejects(() => client.memoryList("p"), /boom/);
});

test("initialize reports the package version as clientInfo.version", async () => {
  const { version } = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  let clientInfo;
  const fakeFetch = async (url, opts) => {
    const body = JSON.parse(opts.body);
    if (body.method === "initialize") clientInfo = body.params.clientInfo;
    const result = body.method === "tools/call" ? { structuredContent: { entries: [] } } : { capabilities: {} };
    return { ok: true, status: 200, headers: new Map([["content-type", "application/json"]]), text: async () => JSON.stringify({ jsonrpc: "2.0", id: body.id ?? null, result }) };
  };
  await createClient({ serverUrl: "https://x/mcp/", apiKey: "arc_k", fetch: fakeFetch }).memoryList("p");
  assert.deepEqual(clientInfo, { name: "archivist-companion", version });
});
