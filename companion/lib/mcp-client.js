// Minimal MCP Streamable-HTTP JSON-RPC client — enough to call read tools.
// The server runs FastMCP(stateless_http=True), so no session is strictly
// required, but we still perform the initialize handshake per the MCP spec and
// forward any Mcp-Session-Id the server returns.

import { createRequire } from "node:module";

const PROTOCOL_VERSION = "2024-11-05";
const { version: PACKAGE_VERSION } = createRequire(import.meta.url)("../package.json");

export function parseMcpBody(contentType, bodyText) {
  if (contentType && contentType.includes("text/event-stream")) {
    // Take the last non-empty `data:` line.
    const dataLines = bodyText
      .split(/\r?\n/)
      .filter((l) => l.startsWith("data:"))
      .map((l) => l.slice(5).trim())
      .filter(Boolean);
    if (dataLines.length === 0) throw new Error("empty SSE body");
    return JSON.parse(dataLines[dataLines.length - 1]);
  }
  return JSON.parse(bodyText);
}

export function createClient({ serverUrl, apiKey, fetch = globalThis.fetch }) {
  let nextId = 1;
  let sessionId = null;

  async function post(method, params, isNotification = false) {
    const headers = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      Authorization: `Bearer ${apiKey}`,
    };
    if (sessionId) headers["Mcp-Session-Id"] = sessionId;
    const body = isNotification
      ? { jsonrpc: "2.0", method, params }
      : { jsonrpc: "2.0", id: nextId++, method, params };
    const res = await fetch(serverUrl, { method: "POST", headers, body: JSON.stringify(body) });
    const sid = res.headers.get ? res.headers.get("mcp-session-id") : null;
    if (sid) sessionId = sid;
    if (!res.ok) throw new Error(`MCP HTTP ${res.status}`);
    if (isNotification) return null;
    const ct = res.headers.get ? res.headers.get("content-type") : "application/json";
    const parsed = parseMcpBody(ct || "application/json", await res.text());
    if (parsed.error) throw new Error(parsed.error.message || "MCP error");
    return parsed.result;
  }

  async function callTool(name, args) {
    await post("initialize", {
      protocolVersion: PROTOCOL_VERSION,
      capabilities: {},
      clientInfo: { name: "archivist-companion", version: PACKAGE_VERSION },
    });
    await post("notifications/initialized", {}, true);
    const result = await post("tools/call", { name, arguments: args });
    if (result && result.structuredContent) return result.structuredContent;
    if (result && Array.isArray(result.content) && result.content[0] && result.content[0].text) {
      return JSON.parse(result.content[0].text);
    }
    return result;
  }

  return {
    callTool,
    memoryList: (project) => callTool("memory_list", { project }),
  };
}
