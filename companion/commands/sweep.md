---
description: Compact all overdue Archivist keywords for this project
argument-hint: "[project-slug]"
disable-model-invocation: true
allowed-tools: mcp__archivist__memory_list mcp__archivist__memory_get mcp__archivist__memory_upsert
---

Sweep pending compactions for project `$ARGUMENTS` (or, if empty, derive the slug
from this repo's git remote — lowercase repo name, drop `.git`, slugify).

1. `memory_list` the project.
2. For every entry with `compaction_due=true` whose `updated_at` is more than 3
   days ago, compact it: `memory_get` the keyword, merge base + all fragments
   into one re-refined content (≤2000 tokens), then `memory_upsert` with the
   `expected_version` and `last_fragment_id` you read.
3. On `VERSION_CONFLICT`, skip that keyword — someone already compacted it.
4. Do not compact entries newer than the 3-day grace window; an in-session
   reader compacts those for free. Report how many you compacted.
