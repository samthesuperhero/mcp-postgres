# mcp-postgres — Architecture

`mcp-postgres` is an [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server,
written in Python, that lets Claude and other AI agents **introspect and manage a PostgreSQL
database** running on the same RHEL-based host. It runs as a `systemd` service under a
dedicated OS user and connects to PostgreSQL locally over `127.0.0.1:5432`.

The defining trait of this service is **privilege self-awareness**: it discovers what it is
actually allowed to do — both at the OS level and inside PostgreSQL — enables only those
operations, and reports the resulting capability tiers back to the calling agent. It
re-verifies these rights before *every* privileged action, so an agent is never told it can
do something it currently cannot (and is notified when its rights change at runtime).

---

## 1. Design goals & how requirements map

| # | Requirement | Where it is addressed |
|---|-------------|-----------------------|
| 1 | Implemented in Python | `mcp_postgres` package (Python 3.11+), official `mcp` SDK, `psycopg` v3 |
| 2 | systemd service on RHEL-based OS | `mcp-postgres.service`, journald logging |
| 3 | Runs as dedicated user `mcp-postgres`; self-checks OS rights, sets modes, reports | §4 OS tier, §5 capability report |
| 4 | With sudo/wheel, may modify **only** `postgresql.conf` & `pg_hba.conf` (hardcoded) | §6 privhelper, double allowlist |
| 5 | DB via IPv4 `127.0.0.1:5432`, user `mcp`, password secret in `/etc/mcp-postgres/` | §3 config & secrets, `db.py` |
| 6 | `mcp` DB privileges out of scope but self-checked (incl. admin); modes set & reported | §4 DB tier, §5 report |
| 7 | Standard MCP API for LLM agents | §2 transport = MCP Streamable HTTP |
| 8 | Prod-side tests run on deploy | §9 self-test suite |

---

## 2. Transport & API surface

- **Protocol:** MCP over **Streamable HTTP** (the current standard HTTP transport in the
  Python MCP SDK). Chosen because the service is a shared, long-lived `systemd` daemon that
  remote AI agents connect to — stdio would require each agent to spawn the process locally.
- **Bind address:** configurable IPv4 + port, default `127.0.0.1:41780`. In the standard
  deployment an **nginx** reverse proxy terminates TLS and forwards to this local upstream;
  remote agents reach the service through nginx, never the app port directly (see §9).
- **Authentication:** by default a static **bearer token** (`Authorization: Bearer <token>`)
  validated by HTTP middleware. The token lives in `/etc/mcp-postgres/token` (mode `0600`).
  Optionally (§2a) an **OAuth 2.1** layer can be turned on for browser clients that cannot send a
  static header — the static token keeps working alongside it (dual auth).

The agent-facing API is the MCP tool catalog (§8). No custom REST API is invented — any
MCP-compatible client (Claude Code, Claude Desktop, etc.) can consume it directly.

<a id="2a"></a>
### 2a. OAuth 2.1 authorization layer (optional)

The claude.ai **web** connector authenticates MCP servers only via OAuth (its Advanced settings
expose an OAuth *Client ID / Secret*, with no field for a static bearer header). So a static-token
server can't attach to the web chat. Turning on `[oauth]` in `config.toml` (with a `public_url`)
fronts `/mcp` with a standards-compliant **OAuth 2.1 Authorization Server + Resource Server**:

- **Discovery & flow** — the server advertises RFC 8414 authorization-server metadata
  (`/.well-known/oauth-authorization-server`) and RFC 9728 protected-resource metadata
  (`/.well-known/oauth-protected-resource/mcp`), supports **dynamic client registration** (RFC 7591,
  `/register`) so claude.ai self-registers with no manual Client ID/Secret, and runs the
  **authorization-code + PKCE (S256)** flow (`/authorize`, `/token`, `/revoke`). An unauthenticated
  request to `/mcp` returns `401` with a `WWW-Authenticate` header pointing at the resource metadata.
- **Who does what** — the **MCP SDK** (`mcp.server.auth`) provides the route handlers and does all
  the protocol work (metadata, DCR validation, PKCE verification, redirect-URI matching, code expiry,
  client authentication). The service supplies only an `OAuthAuthorizationServerProvider`
  implementation: a **sqlite store** (`oauth/store.py`) that persists registered clients and issued
  tokens across restarts, token minting (`oauth/provider.py`), and a **login gate**
  (`oauth/login.py`).
- **The login gate** — because nginx makes `/authorize` publicly reachable, a browser must prove it
  is the owner before a token is issued. `/authorize` parks the (SDK-validated) request in memory and
  redirects the browser to an unauthenticated **`/login`** page; the operator approves by entering the
  **bearer token as the passphrase** (constant-time compared). Only then is a single-use auth code
  minted and the browser redirected back to the client. No new secret is introduced.
- **Dual auth** — the resource-server token check (`load_access_token`) accepts *either* an
  OAuth-issued access token *or* the static configured token (returning a synthetic full-scope grant
  for the latter), so Claude Code / Claude Desktop and the self-test keep working unchanged when OAuth
  is on.
- **Public URLs** — `oauth.public_url` is the externally reachable HTTPS base (what nginx serves). It
  is advertised as the issuer and used to derive the resource identifier `<public_url>/mcp`, so it
  can **not** be the internal `127.0.0.1` bind. nginx must proxy the new root-level paths
  (`/authorize`, `/token`, `/register`, `/revoke`, `/login`, `/.well-known/*`) to the same upstream,
  not just `/mcp`.
- **DNS-rebinding allowlist** — binding `127.0.0.1` makes the MCP SDK auto-enable DNS-rebinding
  protection that accepts only *localhost* `Host` headers, which would reject (HTTP 421 *Misdirected
  Request*) every request nginx forwards with the real public `Host`. When OAuth is on, the server
  therefore passes an explicit `TransportSecuritySettings` that adds `public_url`'s host to the
  allowlist (`server.py:_transport_security`) — so a reverse proxy can keep forwarding the genuine
  `Host` (`proxy_set_header Host $host`) while a spoofed host is still refused.

When `[oauth]` is disabled (the default), none of this is mounted and the endpoint behaves exactly as
before — static bearer token only.

**Self-advertisement.** The server describes itself over the standard MCP discovery
surfaces so an agent needs no prior knowledge (§5a): `instructions` returned at
`initialize`, per-tool **annotations** (`readOnly`/`destructive`/`idempotent` hints) and
titles, and two **resources** — `docs://mcp-postgres/guide` (the capability guide) and
`capabilities://current` (the live report).

---

## 3. Components & filesystem layout

```
┌──────────────┐   MCP / Streamable HTTP (bearer token)   ┌───────────────────────────┐
│  AI agent    │ ───────────────────────────────────────► │  mcp-postgres.service      │
│ (Claude etc.)│                                           │  (user: mcp-postgres)      │
└──────────────┘                                           │                            │
                                                           │  server.py  (FastMCP)      │
                        SELECT pg_reload_conf() (if admin)  │  capabilities.py  guard    │
                    ┌───────────────────────────────────── │  db.py (psycopg pool)      │
                    │                                       │  tools/*                   │
                    ▼                                       └────┬──────────────┬────────┘
        ┌───────────────────────┐         sudo -n (NOPASSWD)    │              │ TCP 127.0.0.1:5432
        │ PostgreSQL server     │ ◄────────────────────────┐    │              ▼  user "mcp"
        │ 127.0.0.1:5432        │                          │    │      ┌───────────────────┐
        └───────────┬───────────┘                          │    └────► │ PostgreSQL (role  │
                    │ owns                                  │           │ mcp)              │
                    ▼                                       ▼           └───────────────────┘
        postgresql.conf / pg_hba.conf        ┌──────────────────────────────┐
                    ▲                         │ privhelper (root-owned)      │
                    └──────── read/write/reload ── │ /usr/libexec/mcp-postgres/   │
                                              └──────────────────────────────┘
```

**Python package `mcp_postgres`** (installed into a venv at `/opt/mcp-postgres/venv`):

| Module | Responsibility |
|--------|----------------|
| `server.py` | Builds the MCP server (FastMCP), applies bearer-token middleware, registers tools per current capability tiers, runs Streamable HTTP. |
| `config.py` | Loads `/etc/mcp-postgres/config.toml`, the DB `secret`, and the auth `token`. |
| `db.py` | `psycopg` v3 connection pool to `127.0.0.1:5432` as role `mcp` (one pool per target database). |
| `manager.py` | Registry of per-database targets (§4a): one `Database` pool + one `CapabilityManager` per database, plus the session-wide *current* target that `use_database` switches. |
| `capabilities.py` | The self-check engine: OS tier + DB tier probes, the capability report, and the per-action `guard`. |
| `privclient.py` | Thin wrapper that shells out to `sudo privhelper …`. |
| `tools/` | MCP tool implementations, grouped and gated by tier. |

**Filesystem layout** (all created by the installer, §7a)

```
<repo>/install.py                          stdlib-only installer, run once by a privileged user
/opt/mcp-postgres/venv/                    application venv + mcp_postgres package
/usr/libexec/mcp-postgres/privhelper       root-owned privileged helper (sudo target)
/etc/mcp-postgres/config.toml              main config          (0640 root:mcp-postgres)
/etc/mcp-postgres/secret                   DB password          (0600 mcp-postgres)
/etc/mcp-postgres/token                    MCP bearer token     (0600 mcp-postgres)
/usr/lib/systemd/system/mcp-postgres.service
/etc/sudoers.d/mcp-postgres                NOPASSWD entry for privhelper ONLY
```

---

## 4. Capability self-check (the core mechanism)

Two independent dimensions are probed. Neither is assumed — both are *measured*.

### OS tier — does `mcp-postgres` have sudo/wheel?
Probe: `sudo -n /usr/libexec/mcp-postgres/privhelper --check`.

| Result | Tier | Effect |
|--------|------|--------|
| exit 0 | `OS_CONFIG` | config-file tools registered & usable |
| non-zero | `OS_NONE` | config-file tools hidden/disabled |

The probe uses `sudo -n` (non-interactive): it succeeds only if a valid NOPASSWD sudoers rule
(or `wheel` membership) is in place. Whether that grant exists is *out of scope* to configure —
the service only observes it.

### DB tier — what can role `mcp` do?
Probe: query `pg_roles` (`rolsuper`, `rolcreatedb`, `rolcreaterole`) and effective read/write
ability for the connected role via `has_*_privilege` / `current_setting`.

The linear tier is driven by **superuser** and write ability only; `createdb`/`createrole` are
**separate capabilities** (see below), so holding them does *not* make the role an admin.

| Observed | Tier | Effect |
|----------|------|--------|
| read only | `DB_READONLY` | introspection + read queries |
| can write DML/DDL | `DB_READWRITE` | + `execute_sql` |
| superuser | `DB_ADMIN` | + `grant` / `revoke` / `admin_sql` |

**DB capabilities (orthogonal to the tier), reported as `db_capabilities`:**

| Attribute | Capability | Enables |
|-----------|------------|---------|
| `CREATEDB` (or superuser) | `createdb` | `create_database` |
| `CREATEROLE` (or superuser) | `createrole` | `create_role` |

So `ALTER ROLE mcp NOSUPERUSER` drops admin while `CREATEDB`/`CREATEROLE` (and all GRANTs)
persist and still enable their own tools.

### Re-check before EVERY action
Rights on the OS user or the `mcp` DB role can change while the daemon runs (someone adds it to
`wheel`, or `GRANT`/`REVOKE`s the role). Therefore a lightweight **`guard`** re-runs the probes
that a given tool needs *immediately before executing it*:

1. Re-probe the relevant tier(s) (sudo check and/or DB-admin query). Results are cached for a
   few seconds so a burst of calls isn't hammered, but every distinct action re-validates.
2. Compare against the last-reported tiers. If they differ, **refresh the registered tool set**
   (add/remove gated tools) and attach a `capability_changed` notice to the tool response, e.g.
   *"OS tier changed OS_NONE → OS_CONFIG"* or *"DB tier changed DB_ADMIN → DB_READWRITE"*.
3. If the tool's required tier is no longer held, refuse with a clear, machine-readable reason
   instead of attempting the action.

This guarantees the agent's view of its own privileges is never stale.

<a id="4a"></a>

### 4a. Target database selection

Role `mcp` is a **cluster-global** PostgreSQL role: the same credentials
(`host`/`port`/`user`/`password`, fixed by config) reach every database in the local cluster
role `mcp` can `CONNECT` to; only the database *name* varies. The service exposes this as a
session-wide **current target database** rather than the single startup database:

- `manager.py`'s `DatabaseManager` holds the base connection config and lazily builds one
  `Database` pool **and one `CapabilityManager` per database**, cached by name (`min_size=0`
  keeps idle pools cheap). The initial current target is `database.dbname` from config.
- **`use_database(name)`** switches the current target. It force-probes the new database first;
  on a connection/permission failure it discards that pool and **leaves the current target
  unchanged**, so a bad name never strands the session. On success it returns that database's
  capability report.
- **The DB tier is measured per database** (e.g. `CREATE` on `public` differs by database), so a
  switch can change the enabled-tool set — which is why each target owns its own
  `CapabilityManager`. The **OS tier is process-global** (the privhelper is not per database) and
  is shared across targets.
- Every tool result carries the current **`database`** so the agent is never in doubt which
  target it acted on; `list_databases` enumerates the names `use_database` accepts.

The current target is process-global (one bearer token → one agent), which suits this
single-tenant deployment; scoping it per MCP session is a possible future refinement.

---

## 5. Capability report

The report is exposed to the agent in two complementary ways:

1. **`get_capabilities` tool** returns the full structured report: the current target
   `database` (§4a), OS tier, DB tier, connected role name & attributes, discovered
   `config_file`/`hba_file` paths, the **`enabled_tools`** list (exactly the tools the current
   tiers permit for that database), any DB error, and a timestamp.
2. **The guard on every gated tool.** All tools are registered so a privilege *gained* mid-session
   is immediately usable without a restart, but each gated tool re-checks its required tier on
   every call (§4) and refuses — with a clear, machine-readable reason plus any
   `capability_changed` notice — when the tier is not currently held. Agents should consult
   `enabled_tools` to know which calls will succeed; calling a disabled tool returns a structured
   refusal rather than acting.

`capability_changed` notices (from §4) keep the agent's view in sync during a session.

<a id="5a"></a>
### 5a. Self-advertisement surfaces

Beyond the report, the server advertises *what it is and how to drive it* using the
standard MCP discovery mechanisms, so a first-time agent can orient itself:

- **`initialize` instructions** — a concise overview (what the service is, the tier model,
  the result envelope, "call `get_capabilities` first") handed to the client at connect
  time via FastMCP's `instructions=`.
- **Tool annotations & titles** — every tool carries a human-readable title and MCP
  `ToolAnnotations` hints (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
  `openWorldHint`) so a client can reason about safety before calling.
- **Resources** — `docs://mcp-postgres/guide` (Markdown capability guide) and
  `capabilities://current` (the live JSON report, same payload as `get_capabilities`).

The advertised prose lives in `docs.py` (single source); resources are registered in
`tools/discovery.py`.

---

## 6. Privileged config-file editing

The **only** privileged action the service performs is editing the two allowlisted files. It is
constrained at two independent layers:

- **App layer** (`privclient.py`): refuses any target whose basename is not
  `postgresql.conf` or `pg_hba.conf`.
- **OS boundary** (`privhelper`, root-owned, the sole `sudo` target): a standalone Python
  script that *hardcodes* the allowed basenames, canonicalizes the requested path
  (`os.path.realpath`), and **refuses anything else** — even if the app layer were bypassed.

`privhelper` subcommands:

| Subcommand | Action |
|------------|--------|
| `--check` | exit 0 (used by the OS-tier probe) |
| `read <file>` | print an allowlisted file's contents |
| `write <file>` | atomically replace an allowlisted file after writing a timestamped `.bak` |
| `reload` | `systemctl reload postgresql` |

Config-file paths are discovered at runtime via `current_setting('config_file')` /
`current_setting('hba_file')` and then re-validated by `privhelper`.

---

## 7. PostgreSQL reload after a config change

Config edits are inert until PostgreSQL re-reads them, so after any successful
`update_postgresql_setting` / `update_pg_hba_rule` the service triggers a **reload**:

1. Preferred: `privhelper reload` → `systemctl reload postgresql` (uses the same sudo the edit
   already required).
2. Fallback: if `systemctl` reload is unavailable but role `mcp` is `DB_ADMIN`, run
   `SELECT pg_reload_conf()`.

Per-call control: `reload = auto | true | false` (default `auto` = reload only on a successful
change). The tool response reports whether a reload happened and **flags settings that require a
full restart** rather than a reload — the service never restarts PostgreSQL on its own; it
surfaces the need so the caller decides. `reload_postgresql` is also available as a standalone
tool.

---

## 8. MCP tool catalog (capability-gated)

| Tool | Min tier | Purpose |
|------|----------|---------|
| `get_capabilities` | always | Full capability report (§5) |
| `health_check` | always | Service up + DB reachable |
| `list_databases` / `list_schemas` / `list_tables` | always | Introspection |
| `describe_table` | always | Columns, PK, indexes, foreign keys (both directions), unique/check constraints, comment, approx row count & size |
| `list_foreign_keys` / `list_indexes` / `list_views` / `list_functions` / `list_enums` | always | Schema-wide introspection (relationship map, indexes, views, routines, enum labels) |
| `get_object_definition` | always | DDL for a view / materialized view / index / function |
| `run_read_query` | always | SELECT in a forced `READ ONLY` transaction (bounded by `statement_timeout`) |
| `explain_query` | always | Query plan; `analyze=True` executes inside the rolled-back READ ONLY txn |
| `sample_table` | always | Preview the first N rows of a table/view |
| `execute_sql` | `DB_READWRITE` | DML / DDL (single statement) |
| `execute_batch` | `DB_READWRITE` | Several statements in one transaction, atomic by default |
| `create_database` | `createdb` capability (or superuser) | Create databases without admin |
| `create_role` | `createrole` capability (or superuser) | Create roles without admin |
| `grant` / `revoke` / `admin_sql` | `DB_ADMIN` (superuser) | Privilege & administrative management |
| `read_postgresql_conf` / `read_pg_hba_conf` | `OS_CONFIG` | Read allowlisted config files |
| `update_postgresql_setting` / `update_pg_hba_rule` | `OS_CONFIG` | Edit (with backup) + auto reload |
| `reload_postgresql` | `OS_CONFIG` (or `DB_ADMIN` fallback) | Reload PostgreSQL config |

`run_read_query` enforces read-only at the transaction level (`SET TRANSACTION READ ONLY`), so
it stays safe even when the role could otherwise write.

---

## 9. Security model

- **Secrets** live only under `/etc/mcp-postgres/` with tight modes (`secret`/`token` = `0600`,
  owned by `mcp-postgres`; `config.toml` = `0640 root:mcp-postgres`). No secret is logged. When OAuth
  is enabled, issued tokens and registered clients persist in a sqlite store at
  `/var/lib/mcp-postgres/oauth.db` (mode `0600`, owned by the service user; systemd
  `StateDirectory=`).
- **Least privilege by measurement:** the service can only do what the *measured* tiers permit,
  re-checked before every action (§4).
- **Config writes are doubly constrained** (§6) and always create a timestamped backup.
- **Transport:** bearer-token auth (optionally OAuth 2.1 as well, §2a — the login gate keeps
  `/authorize` from being an open door, and PKCE binds each code to its client); bind
  `127.0.0.1:41780` by default, fronted by an **nginx** reverse proxy that terminates TLS and is the
  only public entry point (the app port is never exposed directly).
- **systemd hardening:** `User=mcp-postgres`, `ProtectSystem`, `ProtectHome`, journald logging.
  Note: `NoNewPrivileges` must remain **false** — otherwise `sudo`, and therefore config
  editing, would be blocked. This is a deliberate, documented trade-off.

---

## 10. Installer (`install.py`)

Deployment is driven by a single **stdlib-only** Python script, `install.py`, at the repo root.
A privileged user (root, or a sudo-capable admin) clones the repo into their home directory and
runs it once:

```bash
sudo python3 mcp-postgres/install.py --bind 127.0.0.1 --port 41780 --start --run-selftest
```

It requires only the system `python3` (no third-party packages before the venv exists) and is
**idempotent** — safe to re-run to repair an install. For routine code updates on a running host,
use `update.py` (§10a) rather than re-running the full installer.

**Parameters.** Everything is an `argparse` flag: `--bind`, `--port`, `--db-name`,
`--db-user` (default `mcp`), `--grant-wheel`, `--python`, `--start`, `--run-selftest`,
`--create-db-role`. **Secrets are never passed as flags** — the DB password and bearer token
come from an interactive prompt or an env var (`MCP_PG_DB_PASSWORD`, `MCP_PG_TOKEN`); the token
is auto-generated if not supplied.

<a id="7a"></a>**Steps** (in order):

1. **Preflight** — assert `euid == 0`; confirm RHEL-family + `systemd`; confirm `python3 >= 3.11`;
   best-effort check that PostgreSQL answers at `127.0.0.1:5432` (warn, don't fail).
2. **OS user** — create system user `mcp-postgres`
   (`useradd --system --home-dir /opt/mcp-postgres --shell /sbin/nologin`) if missing; with
   `--grant-wheel`, add it to `wheel`.
3. **Files** — create dirs and copy: the `mcp_postgres` package, `privhelper`
   (→ `/usr/libexec/mcp-postgres/`, `root:root 0755`), the systemd unit
   (→ `/usr/lib/systemd/system/`), and the scoped sudoers drop-in
   (→ `/etc/sudoers.d/mcp-postgres`, **validated with `visudo -cf` before install**).
4. **venv** — create `/opt/mcp-postgres/venv` and `pip install` the package (bundled offline
   wheels if present, else PyPI).
5. **Config & secrets** — write `/etc/mcp-postgres/config.toml` from the flags; write `secret`
   and `token` (`0600`, owned by `mcp-postgres`); `config.toml` is `0640 root:mcp-postgres`.
   **Idempotent:** an existing `config.toml`/`secret`/`token` is left untouched on re-run —
   the installer only writes one when it's missing, when the matching env var
   (`MCP_PG_DB_PASSWORD` / `MCP_PG_TOKEN`) supplies a new value, or when `--force` is given
   (which also rotates a generated token). So upgrades never clobber a live password or token.
6. **Optional `--create-db-role`** — connect as the `postgres` superuser and
   `CREATE ROLE mcp LOGIN PASSWORD …` (for operators who have that access; otherwise the role is
   a manual DBA step).
7. **Activate** — `systemctl daemon-reload`; with `--start`, `systemctl enable --now`; with
   `--run-selftest`, run the prod self-test suite (§11) and print the result.

<a id="10a"></a>
### 10a. Updater (`update.py`)

Routine code updates on a live host use a second stdlib-only script, `update.py`, run from the
repo directory (`sudo mcp-postgres/update`). It `import`s `install.py` and reuses its file/venv
steps, adding the two things an *update* needs that a first install does not:

- a **forced** venv reinstall — the pinned package version rarely changes between commits, so a
  plain `pip install <repo>` would report *"already satisfied"* and silently keep the old code;
  `build_venv(force_reinstall=True)` reinstalls the package (`--force-reinstall --no-deps`), then a
  normal install to pull any newly added dependencies;
- a **service restart** (`systemctl restart`) so the new code is actually loaded (the installer
  only `daemon-reload`s and, with `--start`, `enable --now`s);
- an in-place **config migration** (`mcp-postgres-migrate-config`, unless `--no-config-migrate`)
  run *after* the reinstall (so it sees the new schema) and *before* the restart.

Steps: preflight → `git pull --ff-only` **as the checkout owner** (unless `--no-pull`; git refuses
a root pull into a user-owned tree) → refresh files (`lay_down_files`) → forced venv reinstall →
**config migration** → `daemon-reload` + `restart` → self-test (unless `--no-selftest`; a failure
exits non-zero). Because the script imports `install.py` at startup, changes to the installer
scripts themselves land on the *next* run; ordinary app code updates on this run.

**Config migration (`configmigrate.py`).** `config.py` is the single source of truth for the config
schema — the known `[section].key`s and defaults are derived from its dataclasses via
`file_known_keys()`, secrets (`database.password`) are excluded via `SECRET_KEYS`, and removed keys
are recorded in `DEPRECATED_KEYS`. `diff_config()` compares a `config.toml` against that schema;
`apply_migration()` (pure text edits, unit-tested like `confedit.py`) rewrites the file to match:
new keys are added **commented** (so the loader keeps applying their defaults — behavior-neutral,
never pinning the operator to a default that may later change) and retired keys are commented out
with their reason. The entrypoint writes a timestamped `.bak` first and preserves the file's
`0640 root:mcp-postgres` mode/owner; it is idempotent and warn-only (a failure never aborts the
update). Drift is also surfaced passively: the self-test prints a non-fatal `[WARN] config-schema`
line and `server.py` logs one `WARNING config:` line per drift item at startup. **Secrets
(`secret`, `token`) are never touched.**

---

## 11. Prod-side tests (run on deploy)

A `pytest` suite under `tests/prod/`, exposed as a `mcp-postgres-selftest` entrypoint, is run
immediately after `systemctl enable --now`. It validates:

- `systemctl is-active mcp-postgres` reports active.
- MCP `initialize` handshake succeeds over Streamable HTTP with the bearer token.
- DB connectivity as role `mcp` at `127.0.0.1:5432`.
- The `get_capabilities` report matches an independent, direct probe of OS + DB rights.
- The config-file allowlist rejects a disallowed path (negative test).
- `run_read_query` rejects a write statement.
- If `OS_CONFIG`: a round-trip read of `postgresql.conf` succeeds and a backup is produced on
  write (against a scratch setting).

The suite is **read-only / non-destructive by default**; any test that would mutate config runs
only when explicitly enabled and always restores from its backup.
