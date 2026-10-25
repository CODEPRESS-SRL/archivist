# Security Policy

The single source of truth for Archivist's security model: the threat model, the
controls that back it, and the accepted trade-offs. Read it before changing
anything that touches auth, input validation, secret handling, or networking —
and ship a dedicated test with any such change.

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.** Report them
through GitHub Private Vulnerability Reporting:
<https://github.com/CODEPRESS-SRL/archivist/security/advisories/new>. Include
enough detail to reproduce (affected version/commit, steps, and impact). A fix
or mitigation will be coordinated before any public disclosure. Responsible
disclosure is appreciated.

### Supported versions

| Version | Supported |
| ------- | --------- |
| 1.2.x (latest minor) | Yes |
| Older | No |

### Response targets

- Acknowledgement within 5 business days.
- Fix or mitigation plan within 30 days.

## Scope

One deployment profile: **internet-facing VPS**, trusted team, no
anonymous users. Exposed surface: only `443` (front proxy) + `22` (SSH).

**In scope:**

- Unauthorized access to the MCP endpoint (`/mcp`)
- API key theft/leak; revocation
- Injection via tool inputs (keyword, content, FTS query)
- Application-level abuse/DoS (per-key rate limit)
- At-rest data compromise (backups, DB dumps)

**Out of scope for v1 (accepted and documented):**

- Per-project ACLs: any valid key can access any project *within its own
  team* (trusted team) — see "Team tenancy" below for the cross-team
  boundary, which **is** enforced.
- Per-person isolation/attribution within a team: a team's key is shared by
  its members (see "Team tenancy" — shared-key trade-off).
- WAF, real-time audit, MFA: not justified by the scope

## Mandatory controls

| Area | Control |
|---|---|
| Transport | TLS via the front proxy (Caddy/Let's Encrypt or shared nginx/certbot), HSTS, 80→443 redirect |
| Auth | Bearer API key `arc_*`; only SHA-256 stored; revocation via CLI. In service-only mode behind a trusted gateway: `/mcp` refuses every bearer but the service secret (`ARCHIVIST_SERVICE_ONLY`, see below) |
| Network | api and db without host ports in prod; internal Docker network |
| Input | Keyword/project normalized server-side; FTS queries via `plainto_tsquery` (never interpolation); parameters always bound (SQLAlchemy) |
| Rate limit | 120 req/min per key → 429 |
| Container | Non-root user, multi-stage build |
| Secrets | Env only (`.env` on the server, never in the repo); `POSTGRES_PASSWORD` with no default |
| Backups | Daily `pg_dump`; dumps contain plaintext data → same protection level as the DB |
| Audit | JSON log per tool call with `key_owner`, outcome |

## Team tenancy

Archivist segregates the archive per **team**: a `teams` table is the
tenant; every `api_keys` row and every `projects` row points at exactly one
team via `team_id`.

**Invariants:**

- **`team_id` comes from the authenticated token, never from a tool
  parameter.** The auth middleware resolves the caller's key to a
  `(team_id, team_name)` pair and stores it in the request-scoped context;
  every service call takes that `team_id` as an argument. No tool input can
  select or override a team — an agent cannot request another team's data by
  passing a different id/slug.
- **Cross-tenant access returns `NOT_FOUND`, never `FORBIDDEN`.** A lookup
  scoped to `team_id` simply does not match rows belonging to another team,
  so the read/write behaves exactly as if the keyword/project never existed.
  This avoids disclosing that another team's archive exists at all (no
  existence oracle via error-code differences).
- **`scope="all"` stays team-bounded.** Cross-project search widens the
  search to every project *of the caller's own team*, never across teams.
- **Per-team unique project slugs.** `projects` are unique on
  `(team_id, slug)`, not globally — two teams can each have a `billing-api`
  project without collision.

**Shared-team-key trade-off (accepted, documented):** a team's API key is
shared by its members; there is no per-person authentication, revocation, or
attribution inside a team — `updated_by`/`created_by` on writes is the team
name, not an individual. This means:

- A leaked key exposes the **whole team's archive** (read + write) until the
  key is revoked — there is no way to revoke access for a single leaked
  credential while other members keep working, short of issuing everyone a
  new key.
- Audit trails (the JSON log line per tool call) attribute activity to the
  team, not to the individual who made the call.

**Painless rotation** is the mitigation for the shared-key trade-off: because
the tenant is the stable `teams` row (not the key row itself), a compromised
or expiring key can be replaced without touching the team's archive:

1. Issue a new key on the **same team**:
   `key-issue --team <id|name> --label "<member/laptop>"`.
2. Optional grace window: both the old and new key authenticate
   simultaneously while clients switch over.
3. Revoke the old key: `key-revoke --key-id <id>`, or do steps 1+3 atomically
   with `key-rotate --team <id|name> --label "<label>" --revoke-old` (issues
   the new key, then revokes every previously-active key on that team).

See `tests/test_tenancy.py` (cross-team isolation: `get`/`create`/`append`/
`delete`/`search scope=all` all NOT_FOUND-bounded to the caller's team) and
`tests/test_admin_cli.py` (`key-issue`, rotation, `--revoke-old`).

## Service-only mode on /mcp (private stack behind a trusted gateway)

When the api is private and a trusted upstream gateway is the only door to the
memory, set `ARCHIVIST_SERVICE_ONLY=true`. The auth middleware then refuses, on
`/mcp`, **every** bearer that is not `ARCHIVIST_SERVICE_SECRET` — a valid
`arc_` key never reaches the key lookup and gets the canonical 401, byte for
byte identical to a garbage token (no existence oracle). Without the flag the
behaviour is unchanged, so a direct deployment keeps the `arc_` path.

- **Fail-closed and loud.** With the flag ON and `ARCHIVIST_SERVICE_SECRET`
  unset or shorter than 32 characters, the app **refuses to start**: the
  middleware raises while the stack is built, so the container never reports
  healthy. It never falls back to the key path, and never boots healthy while
  refusing every bearer (that would be a silent outage, not an alarm).
- **`/admin` is unaffected**: it keeps its own `arc_` credential and never goes
  through this middleware.
- **What `/admin` is protected by: credentials only.** Do not publish it
  beyond a trusted network (for example a loopback publish reached through an
  SSH tunnel). `FORWARDED_ALLOW_IPS` controls which peers are trusted for
  `X-Forwarded-For`, and the login throttle keys on that value.
- **Premise.** No public vhost for the api, and only the gateway on the
  network the api listens on.
- **Residual:** the `/admin` console reads every project. Its keys follow the
  rule "create for the session, revoke immediately", and a check that no
  `arc_` key is active (`SELECT count(*) FROM api_keys WHERE NOT revoked` = 0)
  belongs in the pre-deploy routine.
- **Residual, structural:** with the flag ON the whole `/mcp` boundary is the
  service secret plus the `X-Archivist-Team` header the gateway sends. A leaked
  secret or a compromised gateway is then worth a leaked `arc_` key, and tenancy
  on that path is decided by the gateway, not here.
- **Threshold that reopens the risk:** an `arc_` key handed to someone without
  root on the host, the `/admin` port exposed beyond a trusted network, a vhost
  for the api, or another container joining the network the api listens on.

Covered by `tests/test_service_auth_unit.py` (both branches, the string → bool
link from the environment, the startup refusal, ordering guard) and
`tests/test_service_auth.py` (end to end, key untouched).

## Admin console (/admin)

- **Auth model**: the team API key IS the login credential — validated with the
  same `lookup_auth_context` as the MCP middleware (SHA-256 hash lookup,
  `revoked = false`). No separate admin credential, no privilege escalation.
- **Tenancy**: `team_id` comes only from the signed session cookie; every read
  and write passes it to the existing team-scoped service functions. A session
  for one team cannot see or mutate another team's data (NOT_FOUND / empty),
  identical to the MCP path.
- **Session cookie**: `archivist_admin`, signed (Starlette `SessionMiddleware`),
  `httponly`, `same_site=strict`, `https_only=true` (served behind Caddy TLS).
- **Session secret**: `ADMIN_SESSION_SECRET`. If unset or empty, an ephemeral
  secret is generated at startup (sessions reset on restart) and a warning is
  logged — set it in production. A secret shorter than 32 characters is used
  as is, but logs a WARNING asking for at least 32 random characters (e.g.
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`); startup is
  never refused, so an existing deployment keeps booting on upgrade. Covered by
  `tests/test_admin_session_secret.py`.
- **CSRF**: every state-changing POST (logout, delete, compact) carries a
  per-session token; a missing/mismatched token is rejected with 400. Logout is
  a POST (never a GET link), so a forged cross-site request cannot end a session.
- **Login throttle**: failed logins are throttled per client IP (in-memory,
  process-local, single-instance — same caveat as the request rate limiter).
  The throttle keys on `request.client.host`, and the app rewrites that from
  `X-Forwarded-For` via uvicorn's `ProxyHeadersMiddleware` (wired in
  `create_app`, so it applies however the app is launched). Trust is scoped to
  `Settings.forwarded_allow_ips` — the proxy/private ranges, **never `*`**: with
  `*` the middleware takes the left-most, client-supplied XFF entry, which a
  spoofed header could rotate to evade the throttle; with the proxy range it
  walks the chain from the right and keys on the real client that Caddy
  appended. Behind Caddy the immediate peer is a Docker-network address (in the
  trusted range) and the forwarded client is public (untrusted), so the throttle
  buckets by the real client instead of collapsing to one global proxy bucket.
- **Reset-on-success trade-off (accepted, documented)**: the throttle counts
  only *failed* logins and clears an IP's counter on a *successful* one, so a
  legitimate user is never locked out by their own earlier typos. The cost: on a
  shared/NAT'd egress IP, each successful login by anyone resets the shared
  bucket, so an attacker interleaving guesses with a co-located user's real
  logins gets a higher effective failure budget than the nominal per-minute
  limit. Accepted because the credential is a ~256-bit key (`arc_` +
  `token_urlsafe(32)`), making online guessing infeasible regardless, and the
  throttle is a best-effort abuse damper (same process-local, single-instance
  caveats as the request rate limiter), not the primary control.
- **Writes**: only soft-delete and naïve compaction; both attributed as
  `admin:<owner_name>` in `memories.updated_by` / `memory_history`.

### Admin console — markdown rendering

Memory content is agent-written free text. The admin console renders it as
markdown (`app/admin/render.py`), which is a stored-XSS surface. Mitigation:
markdown output is sanitized with `nh3` against a conservative tag/attribute
allow-list; raw HTML, `<script>`, event handlers, and non-`http(s)/mailto`
URL schemes are stripped. Covered by `tests/test_admin_markdown.py`.

## Near-duplicate preview and admin reports

Two new read surfaces were added on top of the existing keyword-embedding
column (`memories.keyword_embedding`, migration `0005`): `memory_upsert`'s
`near_duplicates` preview on create, and two read-only admin pages,
`/admin/reports/stale` and `/admin/reports/duplicates`.

- **No new input surface.** Both features query on the keyword, which is
  already validated/normalized server-side before it reaches any of this code
  — there is nothing new to sanitize.
- **Both are team-scoped**, the same way every other query in this codebase
  is: through a `Project.team_id` join, using the `team_id` from the
  authenticated context (the MCP contextvar for `near_duplicates`, the admin
  session for the reports) — never from a request parameter. A row belonging
  to another team simply doesn't match the join and is invisible, same
  NOT_FOUND-shaped behavior as the rest of the tenancy model.
- Covered by `tests/test_dedup_service.py::test_scoped_to_team_and_project`
  (near-duplicate lookup) and `tests/test_admin_reports.py::test_reports_are_team_scoped`
  (both admin reports).

## Companion plugin (client-side)

`companion/` is an optional Claude Code plugin (auto-capture, sweep,
session-start index) that runs entirely on the developer's machine, not on
the server.

- **Key storage**: the team API key lives in `companion.json` under the
  Archivist root — `%LOCALAPPDATA%\Archivist` (Windows),
  `~/Library/Application Support/Archivist` (macOS), `$XDG_DATA_HOME/archivist`
  (Linux); relocate with `ARCHIVIST_HOME`. Plaintext, client-side, written
  `chmod 600`. This is the same exposure as any other MCP client config (e.g.
  `claude mcp add … --header "Authorization: Bearer …"`) — protect the file
  with normal filesystem permissions; if it's copied off the machine, treat
  the key as leaked and rotate it (see "Painless rotation" above).
  `archivist purge` removes it along with the rest of the root.
- **Key never on a command line**: the headless `claude -p` runs (C1 capture,
  C2 sweep) receive their MCP config as a `chmod 600` file staged under
  `<root>/tmp`, not inline in `--mcp-config` — an inline argument is readable
  by any local process through `ps` / Task Manager. An awaited run deletes the
  file immediately; a detached capture cannot (the child is still reading it),
  so its staged copy is reclaimed by the startup garbage collection instead —
  same directory, same permissions, and same exposure as `companion.json`
  itself.
- **Cursor config owner-only**: `archivist-connect` writes the Bearer key
  into `~/.cursor/mcp.json` and sets it to mode `0600` after every write,
  whether it created the file or merged into an existing one (a no-op on
  Windows). Covered by `companion/test/connect.test.js`.
- **No key in a committed project config**: `--scope project` makes
  `claude mcp add` write the header into the project's `.mcp.json`, which is
  meant to be committed. For that scope only, `archivist-connect` registers
  `Authorization: Bearer ${ARCHIVIST_TOKEN}` (a literal reference that Claude
  Code expands from the environment) and tells the user to set
  `ARCHIVIST_TOKEN`. Scopes `user` and `local` keep the key itself in Claude
  Code's per-user config. Covered by `companion/test/connect.test.js`.
- **`--server` validated before use**: `archivist-connect` parses the value
  with `new URL()`, accepts only `http:`/`https:`, and refuses control
  characters, quotes and whitespace before anything is verified or written.
  The Codex `url` is written as a quoted TOML string. Covered by
  `companion/test/connect.test.js`.
- **Executables resolved from PATH only (Windows)**: every process the
  companion spawns (`claude` for registration, install, capture and sweep;
  `git` for the project slug; the `cmd.exe` fallback) resolves its executable
  with `NoDefaultCurrentDirectoryInExePath=1`, so a `claude.exe`, `git.exe`
  or `claude.cmd` planted in the current directory is never run (it would
  otherwise receive the Bearer key in its arguments). Node resolves the
  executable against the parent's environment, so the variable is set in the
  companion's own process for the duration of the spawn call and in the
  child's environment; the working directory is never changed. Covered by
  `companion/test/exe-path.test.js` (a real spawn against a planted
  executable on Windows), `connect.test.js`, `headless.test.js` and
  `slug.test.js`.
- **`claude.cmd` fallback (Windows)**: when `claude` cannot be spawned
  directly, `archivist-connect` runs the npm shim through `cmd.exe`. Before
  that, it refuses any argument holding `% & | ^ < > " !`, CR or LF, and
  doubles trailing backslashes before quoting. Covered by
  `companion/test/connect.test.js`.
- **No secrets in auto-captured memory**: C1 sends a condensed session
  transcript to a headless `claude -p` run whose prompt explicitly forbids
  saving API keys, tokens, passwords, connection strings, or
  environment-variable values (`companion/lib/protocol.js`), covered by
  `companion/test/headless.test.js`. This is a prompt-level control, not a
  content filter on the server — operators should still avoid pasting live
  secrets into sessions that will be auto-captured.
- **Fail-silent by design**: every hook (SessionEnd capture, SessionStart
  index, sweep) exits quietly on any error — server unreachable, key
  missing, `node` absent — and never blocks or delays a session.

## Rules for contributors

1. Never log API keys (not even truncated), nor memory content at INFO
   level.
2. Every new external input (tool parameter, header) → explicit
   validation + a negative test.
3. Migrations contain no data or credentials.
4. New runtime dependencies → justify them in the pull request.
5. Security-relevant changes update this file (and the relevant section) as
   part of the same change.
