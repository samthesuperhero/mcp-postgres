"""Guided recipes, published as MCP Prompts.

Prompts are parameterized *recipes*: each returns a plain-text plan that walks an agent
through a common task using this server's own tools, in the right order and with the right
cautions. They add no new privileges — every step routes through a capability-gated tool
that self-checks — so a prompt always renders; only the tools it names may refuse.

FastMCP turns a function's typed parameters into the prompt's arguments and wraps a
returned ``str`` as a user message (see ``fastmcp/prompts/base.py``). The recipes are pure
functions of their arguments, independent of the DB, so they are trivially testable.
"""

from __future__ import annotations


def register(mcp, ctx) -> None:  # ctx unused: recipes are static text, by design
    @mcp.prompt(
        name="audit_privileges",
        title="Audit database privileges",
        description="Guided, read-only review of who can do what in the current database.",
    )
    def audit_privileges(role: str = "") -> str:
        scope = f" for role '{role}'" if role else ""
        role_filter = f" WHERE grantee = '{role}'" if role else ""
        return f"""\
Audit PostgreSQL privileges on the current target database{scope}. Use only this server's
read-only tools, and change nothing — propose GRANT/REVOKE for the user to decide.

1. `get_capabilities` — note the current database, the connected role, and your DB tier.
2. Roles and their attributes:
   `run_read_query`: SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin,
   rolreplication, rolconnlimit FROM pg_roles ORDER BY rolname
3. Role memberships (inherited privileges):
   `run_read_query`: SELECT r.rolname AS member, g.rolname AS member_of
   FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
   JOIN pg_roles g ON g.oid = m.roleid ORDER BY member, member_of
4. Table/view grants:
   `run_read_query`: SELECT grantee, table_schema, table_name, privilege_type
   FROM information_schema.role_table_grants{role_filter}
   ORDER BY grantee, table_schema, table_name
5. Schema- and database-level access: check `has_schema_privilege` (USAGE/CREATE) and
   `has_database_privilege` (CONNECT/CREATE) for the roles of interest.
6. Summarize{scope}: what each role can read, write, and own. Flag anything over-privileged
   — unexpected superusers, broad PUBLIC grants, login roles with CREATEROLE, etc.

If a change is warranted, propose the exact GRANT/REVOKE and let the user apply it
(`grant`/`revoke` need DB_ADMIN)."""

    @mcp.prompt(
        name="add_column_safely",
        title="Safely add a column",
        description="Inspect a table, then apply an atomic ALTER TABLE with lock/rewrite cautions.",
    )
    def add_column_safely(
        table: str,
        column: str,
        column_type: str,
        default: str = "",
        not_null: bool = False,
    ) -> str:
        ddl = f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
        if default:
            ddl += f" DEFAULT {default}"
        if not_null:
            ddl += " NOT NULL"
        return f"""\
Add column "{column}" ({column_type}) to {table} on the current target database, safely.
Requires DB_READWRITE — confirm with `get_capabilities` first.

1. `describe_table(table="{table}")` — confirm it exists, that "{column}" doesn't already
   exist, and note the approximate row count and size, plus existing constraints.
2. Weigh the lock/rewrite cost. Adding NOT NULL without a default, or a volatile DEFAULT,
   can take a strong lock and rewrite the whole table. On a large table prefer:
   add the column nullable → backfill in batches → then add NOT NULL / constraints.
3. Apply it atomically so nothing half-applies:
   `execute_batch(statements=["{ddl}"], stop_on_error=True)`
   (For the staged approach, run the backfill and the NOT NULL as separate steps.)
4. Verify with `describe_table(table="{table}")` that the column is present with the
   intended type, default, and nullability.

If the table is large or the change could lock it, warn the user with the expected impact
before applying."""

    @mcp.prompt(
        name="investigate_slow_query",
        title="Investigate a slow query",
        description="Read the plan, check indexes/stats, and look for live contention.",
    )
    def investigate_slow_query(query: str) -> str:
        return f"""\
Investigate why this query is slow on the current target database. Use read-only and
observability tools only; change nothing without the user's approval.

QUERY:
{query}

1. `explain_query(sql=<query>, analyze=True, format="json")` — read the plan for sequential
   scans on big tables, large gaps between estimated and actual rows, and expensive
   sorts/hash joins. (ANALYZE runs the query but the transaction is rolled back.)
2. Schema & indexes: `describe_table` / `list_indexes` on the tables involved — is there an
   index supporting the filters and joins? Are the planner's row estimates off (stale stats)?
3. Live contention while it runs: `server_activity` (is it long-running or waiting on an
   event?) and `list_locks` (is it blocked by another backend? note the blocker pid).
4. Relevant settings: `get_settings(name="work_mem")`, `get_settings(name="shared_buffers")`,
   `get_settings(name="random_page_cost")`.
5. Propose concrete fixes — a specific CREATE INDEX, a rewritten query, `ANALYZE` to refresh
   stats, or a setting change — with the expected effect. Apply only if the user approves and
   your tier allows it."""
