const DEFAULT_BUDGET = 1500;

export function formatIndex(entries, budgetChars = DEFAULT_BUDGET) {
  if (!entries || entries.length === 0) return "";
  const header = `Archivist memory for this project (${entries.length} keyword${entries.length === 1 ? "" : "s"}). Search before non-trivial work.`;
  const lines = [];
  let used = header.length;
  let shown = 0;
  for (const e of entries) {
    const line = `- ${e.keyword}${e.compaction_due ? " ⚠ compaction due" : ""}`;
    if (used + line.length + 1 > budgetChars) break;
    lines.push(line);
    used += line.length + 1;
    shown += 1;
  }
  const remaining = entries.length - shown;
  if (remaining > 0) lines.push(`- …and ${remaining} more (use memory_search to explore)`);
  return `${header}\n${lines.join("\n")}`;
}

export function buildStartOutput(project, entries) {
  const additionalContext = formatIndex(entries);
  if (!additionalContext) return null;
  return JSON.stringify({
    hookSpecificOutput: {
      hookEventName: "SessionStart",
      additionalContext,
    },
  });
}
