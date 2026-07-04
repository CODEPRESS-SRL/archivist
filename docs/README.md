# Archivist documentation

Deeper reference for [Archivist](../README.md) — shared, keyword-based memory
for coding agents.

## For agents & integrators

- **[Core concepts](concepts.md)** — the refine → append → compaction →
  retrieve cycle, project namespacing, keyword normalization, caps and
  backpressure. Start here to understand *how the memory works*.
- **[MCP tools reference](mcp-tools.md)** — the six tools (signatures,
  parameters, return contract) and the error codes.

## For operators

- **[Self-hosting guide](self-hosting.md)** — deploy (dedicated host or shared
  proxy), configure, administer teams and keys, run the admin console, back up
  and restore.
- **[Operations runbook](../RUNBOOK.md)** — incident response: service down,
  disk full, compromised key, restore, upgrade.
- **[Security](../SECURITY.md)** — threat model, controls, team tenancy, and how
  to report a vulnerability.

## For contributors

- **[Architecture](architecture.md)** — components, data schema, and the design
  decisions (append-only writes, merge-on-read, why not an event queue, team
  tenancy, hybrid retrieval).
- **[Contributing](../CONTRIBUTING.md)** — dev setup, tests, and conventions.
- **[Changelog](../CHANGELOG.md)** — release history.
