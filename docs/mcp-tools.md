# MCP tools reference

Archivist exposes **six tools** over a single Streamable-HTTP MCP endpoint at
`/mcp`. Every MCP client receives the protocol (the refine → append →
compaction → retrieve cycle) in the `initialize` handshake; see
[concepts.md](concepts.md). This page is the contract: parameters, returns, and
error codes.

All tools take a `project` slug (see
[project namespacing](concepts.md#project-namespacing)). Keywords are normalized
server-side (lowercase, trim, slugify). Every call is scoped to the caller's
team, resolved from the Bearer key — never from a parameter.

## Tool summary

| Tool | Parameters | Purpose |
|---|---|---|
| [`memory_search`](#memory_search) | `project, query, limit=5, scope="project", intent="unspecified"` | Find entries by keyword + full text |
| [`memory_get`](#memory_get) | `project, keywords[]` | Read entries: base + fragments + version |
| [`memory_append`](#memory_append) | `project, keyword, fragment` | Queue a fragment on an existing entry (default write) |
| [`memory_upsert`](#memory_upsert) | `project, keyword, content, expected_version?, last_fragment_id?` | Create an entry, or write a compaction result |
| [`memory_list`](#memory_list) | `project` | List every keyword in a project |
| [`memory_delete`](#memory_delete) | `project, keyword` | Soft-delete an entry (history preserved) |

---

### `memory_search`

Search shared memory by exact/prefix keyword and full text over content **and**
pending fragments. Search before starting non-trivial work or when you hit an
unknown entity.

**Parameters**

- `project` (str) — project slug.
- `query` (str) — search terms.
- `limit` (int, default 5) — max results.
- `scope` (str, default `"project"`) — `"project"` searches the current
  project; `"all"` searches every project in the team (each result carries its
  own `project`).
- `intent` (str, default `"unspecified"`) — `"read"` for a genuine retrieval,
  `"write"` for the pre-create dedup probe. **Observational only** — an audit
  signal that never affects results; an unrecognized value is coerced to
  `"unspecified"` and never fails the search.

**Returns** — ranked `results` (each with `keyword`, `excerpt`, `score`,
`pending_fragments`, `token_count`, `age_days`) plus `related_keywords` to reuse
instead of creating duplicates.

---

### `memory_get`

Read entries by keyword.

**Parameters**

- `project` (str)
- `keywords` (str | list[str]) — one or more keywords to read. A single
  keyword may be passed as a bare string (`"design-system"`) or a list
  (`["design-system"]`); both are accepted.

**Returns** — for each found keyword: base `content`, pending `fragments`,
`fragment_count`, `version`, `last_fragment_id`, `compaction_due`,
`token_total`, and `age_days` (days since the entry was last written). Unknown
keywords come back in a `not_found` list.

> If `compaction_due` is true, **compact now**: merge base + all fragments into
> one refined content and call [`memory_upsert`](#memory_upsert) with the
> returned `version` (as `expected_version`) and `last_fragment_id`.

---

### `memory_append`

Append a refined fragment to an **existing** entry — the default write path once
a keyword exists. Never blocks, never conflicts.

**Parameters**

- `project` (str)
- `keyword` (str) — must already exist (else `NOT_FOUND`; create with
  `memory_upsert`).
- `fragment` (str) — refined, ≤ 500 tokens.

**Returns** — the append result, including `compaction_due`. When it is `true`,
the entry crossed the threshold: read it with `memory_get` and compact via
`memory_upsert`.

**Errors** — `FRAGMENT_TOO_LARGE` (> 500 tokens); `TOO_MANY_FRAGMENTS` (10
pending — compact first); `NOT_FOUND` (keyword absent).

---

### `memory_upsert`

Create a new entry, **or** write a compaction result. The presence of
`expected_version` selects the mode.

**Parameters**

- `project` (str)
- `keyword` (str)
- `content` (str) — refined content. 2000-token **soft cap**: over it the write
  still succeeds but the response carries `soft_cap_exceeded: true` and a
  `warning`. 10000-token **hard cap**: over it the write is rejected
  (`ENTRY_TOO_LARGE`).
- `expected_version` (int, optional) — **omit to create** (entry is created at
  version 1); **pass to compact** (must match the current version).
- `last_fragment_id` (int, optional) — on a compaction, the id up to which
  fragments are consumed. **Required whenever fragments are pending**, so the
  fragments you merged are consumed atomically and later ones survive.

**Returns** — the created/updated entry with its new `version`. On **create**,
the response may also carry `near_duplicates`: existing keywords in the same
project whose keyword embedding is close to this one (each with `keyword`,
`similarity`, `preview`). It is advisory only — never a rejection — and is
omitted when there's no close match or the embedder is unavailable. If one of
them is really the same concept, self-correct: `memory_delete` the entry just
created and `memory_append` a fragment to the existing keyword instead. When the
content is over the 2000-token soft cap (but under the 10000 hard cap), the
response includes `soft_cap_exceeded: true` (bool) and `warning` (str).

**Errors** — `ALREADY_EXISTS` (created a keyword that already exists — read +
append, or compact); `ENTRY_TOO_LARGE` (> 10000 tokens — split the concept);
`VERSION_CONFLICT` (someone compacted first — re-read, merge, retry);
`VALIDATION_ERROR` (compaction omitted `last_fragment_id` while fragments were
pending).

---

### `memory_list`

List every keyword in a project with `token_count`, `pending_fragments`,
`compaction_due`, and `updated_at`. Use it to get oriented in a project.

**Parameters** — `project` (str).

---

### `memory_delete`

Soft-delete an entry; history is preserved.

**Parameters** — `project` (str), `keyword` (str).

**Returns** — `deleted=true`, or `NOT_FOUND` if the keyword is not present.

---

## Error envelope

Rejections are never raised as raw MCP exceptions. They come back as a
structured envelope whose message tells the agent what to do next:

```json
{ "error": { "code": "VERSION_CONFLICT", "message": "Version mismatch: expected 3, current is 4. Someone compacted meanwhile. Re-read with memory_get, merge again, retry memory_upsert.", "current_version": 4 } }
```

| Code | Meaning / next step |
|---|---|
| `UNAUTHORIZED` | Invalid or missing API key — pass `Authorization: Bearer arc_…`. |
| `RATE_LIMITED` | > 120 req/min per key — wait a few seconds and retry. |
| `NOT_FOUND` | Keyword absent in the project — create it with `memory_upsert`. |
| `ALREADY_EXISTS` | Created a keyword that exists — read + `memory_append`, or compact. |
| `ENTRY_TOO_LARGE` | Content > 10000 tokens (hard cap) — split the concept across keywords. Over the 2000 soft cap is accepted with a `soft_cap_exceeded` warning, not this error. |
| `FRAGMENT_TOO_LARGE` | Fragment > 500 tokens — refine down to essentials. |
| `TOO_MANY_FRAGMENTS` | 10 pending fragments — compact first (backpressure). |
| `VERSION_CONFLICT` | Compaction lost the race — re-read, merge, retry. |
| `VALIDATION_ERROR` | Bad input (e.g. compaction missing `last_fragment_id`). |

The exact codes and message text live in
[`api/app/errors.py`](../api/app/errors.py) — the single source of truth.
