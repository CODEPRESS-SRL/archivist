import * as nodeFs from "node:fs";
import { join } from "node:path/posix";

export const BEGIN = "<!-- archivist:begin (managed by archivist-connect — edits inside are overwritten) -->";
export const END = "<!-- archivist:end -->";

export const RULE = `${BEGIN}
## Archivist shared memory
- Before non-trivial work, search Archivist first (\`memory_search\`).
- Save durable, reusable context as you learn it (\`memory_upsert\` / \`memory_append\`).
- The Archivist MCP tools carry the full refine→append→compaction protocol.
${END}`;

// Insert or replace the marker-delimited block. Returns new content.
export function upsertManagedBlock(content, block = RULE) {
  const cur = content || "";
  const start = cur.indexOf(BEGIN);
  if (start !== -1) {
    const endIdx = cur.indexOf(END, start);
    if (endIdx !== -1) {
      const after = endIdx + END.length;
      return cur.slice(0, start) + block + cur.slice(after);
    }
  }
  if (cur.trim() === "") return block + "\n";
  const sep = cur.endsWith("\n") ? "\n" : "\n\n";
  return cur + sep + block + "\n";
}

const AGENTS = [
  { key: "claude", dir: ".claude", file: "CLAUDE.md" },
  { key: "codex", dir: ".codex", file: "AGENTS.md" },
];

// Only touches a user-level rules file when the agent's home dir exists.
// Creates the file if absent; writes only when content changes. Never throws
// for a missing agent dir.
export function writeAgentRules({ home, fs = nodeFs, rule = RULE }) {
  const result = { claude: false, codex: false };
  for (const a of AGENTS) {
    const dirPath = join(home, a.dir);
    if (!fs.existsSync(dirPath)) continue;
    const filePath = join(dirPath, a.file);
    const cur = fs.existsSync(filePath) ? fs.readFileSync(filePath, "utf8") : "";
    const next = upsertManagedBlock(cur, rule);
    if (next !== cur) {
      fs.writeFileSync(filePath, next);
      result[a.key] = true;
    }
  }
  return result;
}
