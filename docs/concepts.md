# Core concepts

Archivist is shared, keyword-addressed memory for the coding agents on a team.
This page explains the mental model an agent (or a person reading its output)
needs. The protocol itself ships to every client in the MCP `initialize`
handshake — nothing here has to be configured.

## The cycle

```
   ┌─────────┐     ┌──────────────┐     ┌───────────────────┐     ┌──────────┐
   │ REFINE  │ ──▶ │    WRITE     │ ──▶ │    COMPACTION     │ ──▶ │ RETRIEVE │
   │ (agent) │     │ append-only  │     │  merge-on-read    │     │  search  │
   └─────────┘     └──────────────┘     └───────────────────┘     └──────────┘
```

### 1. Refine (the agent's job)

Before saving a concept, the agent extracts **1–3 keywords** and produces a
**dense, self-contained mini-context** — compressed, factual, readable months
later without the current session. The server never does this; there is no
server-side LLM. Quality of memory is quality of refinement.

### 2. Write (append-only, event-style)

- **Keyword is known to exist** → `memory_append` queues a refined
  **fragment** (≤ 500 tokens) straight away. Never blocks, never conflicts.
- **New concept** → `memory_upsert` creates the entry at version 1 with the
  mini-context (≤ 2000 tokens).

The pre-write probe (`memory_search` with `intent="write"`) is now **optional**,
not a required step: `memory_upsert` itself checks for near-duplicates on
create and, if it finds one, returns `near_duplicates` (existing keywords that
look like the same concept, with a similarity score and a content preview).
When that happens the agent self-corrects in the same turn — `memory_delete`
the entry it just created, `memory_append` a fragment to the existing keyword
instead — rather than leaving two entries for the same concept. `near_duplicates`
is advisory only: it is never a rejection, and it is omitted entirely when
nothing matches.

Appends are **events, not rewrites**: they never block and never conflict. Two
teammates can append to the same keyword simultaneously and both succeed.

### 3. Compaction (merge-on-read)

Fragments pile up under a keyword until a read surfaces `compaction_due=true`.
At that moment the reading agent — who already holds the content in context —
**is** the responsible party: it merges base + all fragments into one
re-refined value and writes it back with `memory_upsert`, passing the
`expected_version` and `last_fragment_id` it just read.

- It is **safe and reversible** — every version is kept in `memory_history`.
- It is **atomic**: only fragments up to `last_fragment_id` are consumed; a
  fragment appended mid-merge survives to the next round.
- A `VERSION_CONFLICT` means someone compacted first — re-read and retry.

The protocol instructs agents to compact **in the same turn if they can** —
without asking the user first, since it is routine upkeep, not a decision. An
agent mid-task that cannot stop may defer: the safety net for fragments nobody
gets around to compacting is the **archivist-companion** plugin's
`/archivist-companion:sweep` command — a manual slash command that reconciles
overdue compactions on demand, plus a schedulable `sweep-cli.js` for teams that
want it to run on a cron. Both remain optional: a team without the plugin still
relies on in-session compaction, and a deferred compaction is safe either way
since every version is kept and the append queue is bounded.

### 4. Retrieve

Search before any non-trivial task, and whenever an unknown entity (module,
tool, error, convention) appears. `memory_search` with `intent="read"` returns
ranked hits plus `related_keywords` to follow; `memory_get` pulls the entries
that matter; `memory_list` orients you in a new project. If the current project
has nothing and the concept is cross-cutting (tooling, infra, team conventions),
retry with `scope="all"` to search every project in the team.

Both `memory_search` hits and `memory_get` entries carry `age_days` (and
`memory_get` also carries `fragment_count`) — how long since the entry was last
written. Treat an old entry with a little more scrutiny, and **correct on
contradiction**: if a memory disagrees with what you observe in the code right
now, don't just note the discrepancy — fix it in the same turn with a
correction fragment (`memory_append`). Freshness is not itself a ranking
signal; this is about the reading agent's judgment, not the server's.

## Project namespacing

Every tool takes a `project` slug. Memory is shared by the whole team under it.
The slug is derived deterministically so teammates converge on the **same**
namespace with zero coordination:

> Take the repository name from the git remote, lowercase it, drop the `.git`
> suffix, and slugify.
>
> `git@github.com:acme/Billing-API.git` → `billing-api`

With no remote, normalize the root directory name instead. **Never invent or
abbreviate the slug** — teammates who pick different names split the namespace
and never see each other's memory.

## Keyword normalization

Keywords are normalized **server-side**: lowercase, trimmed, slugified
(`Redis Cache` → `redis-cache`). This kills trivial duplicates at the source.
`memory_search` additionally returns `related_keywords` so an agent reuses an
existing concept instead of creating a near-duplicate.

## Caps and backpressure

Bounds that keep entries dense and queues finite (all configurable — see
[self-hosting.md](self-hosting.md#configuration)):

| Bound | Default | Effect when exceeded |
|---|---|---|
| Entry size | 2000 tokens | `memory_upsert` rejects → compress or split the concept |
| Fragment size | 500 tokens | `memory_append` rejects → refine down to essentials |
| Compaction threshold | > 3 fragments, or base+fragments > 2/3 of the cap | sets `compaction_due=true` |
| Hard fragment limit | 10 pending | `memory_append` rejects → compact first (backpressure) |

Tokens are counted with **tiktoken** (`cl100k_base`), never a character
heuristic.

## What not to save

The protocol tells agents to skip: **secrets or credentials**; framework/library
trivia; and anything recoverable from the code in ~10 seconds. Memory is for
hard-won, team-relevant context — not a cache of the obvious.

## See also

- [mcp-tools.md](mcp-tools.md) — the exact tool signatures and return contract
- [architecture.md](architecture.md) — how the cycle is implemented
