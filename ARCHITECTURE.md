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
- **Bind address:** configurable IPv4 + port, default `127.0.0.1:8080`. For remote access,
  front it with a TLS-terminating reverse proxy (see §7).
- **Authentication:** a static **bearer token** (`Authorization: Bearer <token>`) validated by
  HTTP middleware. The token lives in `/etc/mcp-postgres/token` (mode `0600`).

The agent-facing API is the MCP tool catalog (§8). No custom REST API is invented — any
MCP-compatible client (Claude Code, Claude Desktop, etc.) can consume it directly.

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
| `db.py` | `psycopg` v3 connection pool to `127.0.0.1:5432` as role `mcp`. |
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

| Observed | Tier | Effect |
|----------|------|--------|
| read only | `DB_READONLY` | introspection + read queries |
| can write DML/DDL | `DB_READWRITE` | + `execute_sql` |
| superuser / createdb / createrole | `DB_ADMIN` | + role/db management, `admin_sql` |

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

---

## 5. Capability report

The report is exposed to the agent in two complementary ways:

1. **`get_capabilities` tool** returns the full structured report: OS tier, DB tier, connected
   role name & attributes, discovered `config_file`/`hba_file` paths, the **`enabled_tools`**
   list (exactly the tools the current tiers permit), any DB error, and a timestamp.
2. **The guard on every gated tool.** All tools are registered so a privilege *gained* mid-session
   is immediately usable without a restart, but each gated tool re-checks its required tier on
   every call (§4) and refuses — with a clear, machine-readable reason plus any
   `capability_changed` notice — when the tier is not currently held. Agents should consult
   `enabled_tools` to know which calls will succeed; calling a disabled tool returns a structured
   refusal rather than acting.

`capability_changed` notices (from §4) keep the agent's view in sync during a session.

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
| `describe_table` | always | Columns, types, indexes, constraints |
| `run_read_query` | always | SELECT in a forced `READ ONLY` transaction |
| `execute_sql` | `DB_READWRITE` | DML / DDL |
| `create_database` / `create_role` / `grant` / `revoke` / `admin_sql` | `DB_ADMIN` | Role & database management |
| `read_postgresql_conf` / `read_pg_hba_conf` | `OS_CONFIG` | Read allowlisted config files |
| `update_postgresql_setting` / `update_pg_hba_rule` | `OS_CONFIG` | Edit (with backup) + auto reload |
| `reload_postgresql` | `OS_CONFIG` (or `DB_ADMIN` fallback) | Reload PostgreSQL config |

`run_read_query` enforces read-only at the transaction level (`SET TRANSACTION READ ONLY`), so
it stays safe even when the role could otherwise write.

---

## 9. Security model

- **Secrets** live only under `/etc/mcp-postgres/` with tight modes (`secret`/`token` = `0600`,
  owned by `mcp-postgres`; `config.toml` = `0640 root:mcp-postgres`). No secret is logged.
- **Least privilege by measurement:** the service can only do what the *measured* tiers permit,
  re-checked before every action (§4).
- **Config writes are doubly constrained** (§6) and always create a timestamped backup.
- **Transport:** bearer-token auth; bind `127.0.0.1` by default; use a reverse proxy for TLS
  when exposing remotely.
- **systemd hardening:** `User=mcp-postgres`, `ProtectSystem`, `ProtectHome`, journald logging.
  Note: `NoNewPrivileges` must remain **false** — otherwise `sudo`, and therefore config
  editing, would be blocked. This is a deliberate, documented trade-off.

---

## 10. Installer (`install.py`)

Deployment is driven by a single **stdlib-only** Python script, `install.py`, at the repo root.
A privileged user (root, or a sudo-capable admin) clones the repo into their home directory and
runs it once:

```bash
sudo python3 mcp-postgres/install.py --bind 127.0.0.1 --port 8080 --start --run-selftest
```

It requires only the system `python3` (no third-party packages before the venv exists) and is
**idempotent** — safe to re-run to upgrade or repair an install.

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
