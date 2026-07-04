// Shared refine + DO-NOT-SAVE contract, embedded verbatim in the capture and
// merge prompts so a headless run follows the same rules a human-driven agent
// would. Mirrors the server's MCP instructions.
export const PROTOCOL = `You are writing to Archivist, the team's shared keyword memory, via its MCP tools.
Derive the project slug from the git remote (lowercase repo name, drop .git, slugify); never invent it.
Refine before writing: dense, factual, self-contained, English. Extract 1-3 keywords per concept.
If a concept's keyword exists, memory_append a fragment (<=500 tokens); if new, memory_upsert a mini-context (<=2000 tokens).
If a memory_upsert create returns near_duplicates that are the same concept, delete your new entry and append to the existing keyword instead.
NEVER save secrets or credentials: no API keys, tokens, passwords, connection strings, or environment-variable values. Skip anything recoverable from the code in ~10 seconds.`;

// C2 sweep: the prompt for a single non-detached headless run that merges one
// overdue keyword. Reuses PROTOCOL so the merge run follows the same rules.
export function buildMergePrompt(slug, keyword, protocol) {
  return `${protocol}

The project slug is "${slug}". Compact the keyword "${keyword}".

Steps: memory_get "${keyword}"; merge its base content and all pending fragments
into one re-refined content (<=2000 tokens); then memory_upsert with the
expected_version and last_fragment_id you just read. If you get VERSION_CONFLICT,
someone else already compacted it — stop, that entry is done. Do not touch any
other keyword.`;
}
