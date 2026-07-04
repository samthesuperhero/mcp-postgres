"""Self-advertisement copy: what mcp-postgres is and how to drive it.

This module holds only static strings (plus a best-effort version lookup) so the
server and the discovery resources share one source of truth. The prose here is
delivered to agents two ways:

* ``SERVER_INSTRUCTIONS`` — handed to the client in the MCP ``initialize``
  response (FastMCP ``instructions=``); the first thing an agent sees.
* ``GUIDE_MARKDOWN`` — the full catalog, exposed as the ``docs://mcp-postgres/guide``
  resource for an agent that wants details.
"""

from __future__ import annotations

from importlib import metadata

REPO_URL = "https://github.com/samthesuperhero/mcp-postgres"

GUIDE_URI = "docs://mcp-postgres/guide"
CAPABILITIES_URI = "capabilities://current"


def version() -> str | None:
    """Installed package version, or ``None`` if metadata is unavailable."""
    try:
        return metadata.version("mcp-postgres")
    except metadata.PackageNotFoundError:
        return None


SERVER_INSTRUCTIONS = f"""\
mcp-postgres is a privilege-aware MCP server for managing PostgreSQL on the local
host (role `mcp` over 127.0.0.1:5432). Role `mcp` is cluster-global, so you can act
on ANY database in the cluster it can connect to — not just one.

START HERE
- Call `get_capabilities` (or read the `{CAPABILITIES_URI}` resource) before acting.
  It reports the current target database, the privilege tiers, the exact
  `enabled_tools` you may use right now, and an `environment` block describing what you
  are actually talking to — the PostgreSQL version, the extensions (activated / available
  / preloaded), and the host OS. For the full catalog and semantics, read `{GUIDE_URI}`.

CHOOSING A DATABASE
- Everything acts on the current target database (`database` in every result).
- `list_databases` lists the databases you can target; `use_database(name)` switches
  the current target (session-wide) and returns the capability report for it. The DB
  tier is measured per database, so it may change when you switch. On a bad name the
  current target is left unchanged.

PRIVILEGE TIERS (what the service is allowed to do is *measured*, not assumed)
- DB tier:  DB_READONLY < DB_READWRITE < DB_ADMIN  (privileges of role `mcp` in the
  current database)
- OS tier:  OS_NONE < OS_CONFIG  (whether the service may edit postgresql.conf /
  pg_hba.conf and reload PostgreSQL via sudo — cluster-global, not per database)
Tiers are re-checked before EVERY call. A tool may be advertised but still refuse
if the required tier is not currently held — PostgreSQL remains the final authority.

RESULT ENVELOPE (every tool returns a JSON object)
- `ok`: true on success, false on refusal or error.
- `database`: the current target database this result came from.
- `error`: present when `ok` is false (e.g. insufficient tier, or a SQL error).
- `capability_changed`: present on any result when your privileges shifted since the
  last call (e.g. "DB tier changed DB_READONLY -> DB_READWRITE"); use it to re-read
  `get_capabilities`.

SAFETY
- `run_read_query` always runs inside a forced READ ONLY transaction — safe even if
  role `mcp` can write.
- Mutating, admin, and config-file tools carry MCP annotations (readOnly / destructive
  / idempotent hints); prefer read-only tools unless a change is intended.
- Config-file edits go through a two-file allowlist and always write a timestamped
  backup; some settings need a full PostgreSQL restart (flagged, never done for you).

STAY WITHIN THESE TOOLS
- Manage PostgreSQL only through the tools `get_capabilities` lists, each for its stated
  purpose. If a tool errors or the required tier is missing, report that to the user —
  do NOT substitute another tool to route around it.
- Never use `admin_sql` / `ALTER SYSTEM` to do a config-file tool's job. Such bypasses
  skip this server's safety net (two-file allowlist, timestamped backups,
  restart-vs-reload flagging) and can silently misconfigure the cluster.
- If a workaround is truly unavoidable, WARN the user first — name the guardrail you
  would bypass and the risk — and proceed only with their consent.
"""


GUIDE_MARKDOWN = f"""\
# mcp-postgres — capability guide

A privilege-aware [MCP](https://modelcontextprotocol.io) server that lets an AI agent
introspect and manage **the local PostgreSQL cluster** (role `mcp`, `127.0.0.1:5432`) —
**any database** role `mcp` can connect to, one at a time. Source: <{REPO_URL}>.

## How to use it

1. Call **`get_capabilities`** first (or read the `{CAPABILITIES_URI}` resource). It
   returns the current target **`database`**, the live OS/DB tiers, the connected role
   and its attributes, and the exact **`enabled_tools`** you may use right now.
2. Call tools from that list. Every tool re-checks its required tier immediately before
   acting, so a tool that is advertised may still refuse if your rights changed.

## Choosing the target database

Role `mcp` is a cluster-global PostgreSQL role, so the service can act on any database
in the cluster it may `CONNECT` to (only the database name varies — host, port, and
credentials are fixed by the deployment). All tools operate on a session-wide **current
target database**, reported as `database` in every result.

- **`list_databases`** — the databases you can target.
- **`use_database(name)`** — switch the current target; returns that database's capability
  report. The **DB tier is measured per database**, so it can differ after a switch; a
  bad or unreachable name leaves the current target unchanged.

## Privilege tiers

The service **measures** what it may do, on two independent axes:

| Axis | Tiers (low → high) | Meaning |
|------|--------------------|---------|
| DB   | `DB_READONLY` → `DB_READWRITE` → `DB_ADMIN` | privileges of role `mcp` in the current database |
| OS   | `OS_NONE` → `OS_CONFIG` | may the service edit `postgresql.conf`/`pg_hba.conf` and reload, via sudo |

Tiers are re-checked before every action; if they change mid-session, the affected tool
response carries a `capability_changed` notice.

## Result envelope

Every tool returns a JSON object:

- `ok` — `true` on success, `false` on refusal/error.
- `database` — the current target database the result came from.
- `error` — a message when `ok` is `false` (insufficient tier, SQL error, …).
- `capability_changed` — a list of change notices, present on any result when your
  privileges shifted since the previous call.

## Environment

`get_capabilities` (and the `{CAPABILITIES_URI}` resource) also carries an `environment`
block describing the concrete cluster and host — read it to know what you are talking to
before choosing SQL syntax or features:

- `environment.postgresql` — `version_string` (full `SELECT version()`), `server_version`,
  numeric `version_num`, and `major`.
- `environment.postgresql.extensions` — three parallel lists: `activated` (extensions
  created in the current database, name + version), `available` (present on disk but not
  yet created here, name + default_version), and `preloaded_libraries`
  (`shared_preload_libraries`, loaded into the server). A preload-only module has no SQL
  extension, and an extension can be activated without being preloaded — hence three lists.
- `environment.os` — host OS `family`, `name`, `version`, `pretty_name`, `kernel`, `arch`.
  The service runs on the PostgreSQL host, so this is the cluster's OS.

The PostgreSQL version and OS are fixed for the process; `extensions.activated` reflects the
**current** database and changes when you `use_database`.

## Tool catalog (capability-gated)

### Always available
- `get_capabilities` — full capability report (current database, tiers, role, enabled
  tools, timestamp).
- `health_check` — service up and the current database reachable.
- `use_database` — switch the current target database (same cluster, role `mcp`); returns
  the new database's capability report.
- `list_databases`, `list_schemas`, `list_tables`, `describe_table` — read-only introspection
  (`list_databases` also enumerates the names `use_database` accepts).
- `run_read_query` — run a `SELECT`/read in a forced **READ ONLY** transaction (safe even
  when role `mcp` can write).

### Requires `DB_READWRITE`
- `execute_sql` — run a DML/DDL statement.

### Requires `DB_ADMIN`
- `create_database`, `create_role` — provision databases/roles.
- `grant`, `revoke` — change privileges.
- `admin_sql` — run an arbitrary administrative statement.

### Requires `OS_CONFIG` (sudo to the privhelper)
- `read_postgresql_conf`, `read_pg_hba_conf` — read the two allowlisted config files.
- `update_postgresql_setting` — set a `postgresql.conf` parameter (backup + reload).
- `update_pg_hba_rule` — append a `pg_hba.conf` rule (backup + reload).
- `reload_postgresql` — reload PostgreSQL config (also allowed via `DB_ADMIN` fallback).

## Operational notes

- **Config edits are doubly constrained**: only `postgresql.conf` and `pg_hba.conf` may be
  touched, enforced independently in-app and in the root-owned privhelper. Every write
  leaves a timestamped `.bak`.
- **Reload vs restart**: after a successful config change the service reloads PostgreSQL
  (`reload = auto|true|false`, default `auto`). Settings that require a full restart are
  flagged in the response; the service never restarts PostgreSQL on its own.
- **Duplicate- and shadow-aware writes**: PostgreSQL honours the *last* uncommented line
  for a setting, so `update_postgresql_setting` edits that effective line and comments out
  any earlier duplicates (reported as `duplicates_disabled`), leaving one unambiguous
  setting. It also checks `pg_settings.sourcefile`: if the value is really coming from
  another file (e.g. `postgresql.auto.conf` from `ALTER SYSTEM`, or a later `include`), the
  response's `note` warns that the edit is shadowed and names the file that must change.
- **Do not bypass the config tools.** Setting configuration by other means — e.g.
  `admin_sql` running `ALTER SYSTEM` — skips the allowlist/backup/restart-flagging and is
  a known footgun: `ALTER SYSTEM SET shared_preload_libraries = 'a,b'` stores the whole
  string as ONE quoted element and PostgreSQL then fails to start. Prefer
  `update_postgresql_setting` (it writes `postgresql.conf` correctly); if ALTER SYSTEM is
  truly necessary, warn the user first and pass list values as separate items
  (`= 'a', 'b'`).
"""
