import { readFileSync } from "node:fs";

const EDIT_TOOLS = new Set(["Edit", "Write", "NotebookEdit"]);

// Read a Claude Code session transcript (JSONL) into condensed text + metrics.
// Never throws: a missing/unreadable file returns zeroed metrics.
export function readTranscript(path, read = (p) => readFileSync(p, "utf8")) {
  const empty = { lineCount: 0, charCount: 0, text: "", fileEdits: 0 };
  try {
    const raw = read(path);
    if (typeof raw !== "string") return empty;
    const lines = raw.split(/\r?\n/).filter(Boolean);
    const texts = [];
    let fileEdits = 0;
    for (const line of lines) {
      let obj;
      try {
        obj = JSON.parse(line);
      } catch {
        continue;
      }
      const content = obj?.message?.content ?? obj?.content;
      if (typeof content === "string") {
        texts.push(content);
      } else if (Array.isArray(content)) {
        for (const c of content) {
          if (c?.type === "text" && c.text) texts.push(c.text);
          if (c?.type === "tool_use" && EDIT_TOOLS.has(c.name)) fileEdits += 1;
        }
      }
    }
    const text = texts.join("\n");
    return { lineCount: lines.length, charCount: text.length, text, fileEdits };
  } catch {
    return empty;
  }
}
