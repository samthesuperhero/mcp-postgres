"""Always-available tools: capability report, health, and read-only introspection."""

from __future__ import annotations

from mcp.types import ToolAnnotations

from ..capabilities import CapabilityError, DbTier
from .base import attach, guard_or_error, rows_as_dicts

# All tools in this module only read; none touch anything outside the local DB.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def register(mcp, ctx) -> None:
    @mcp.tool(title="Get capabilities", annotations=READ_ONLY)
    def get_capabilities() -> dict:
        """Report the current target database, OS/DB privilege tiers, and enabled tools."""
        t = ctx.manager.current_target()
        return t.caps.report(database=t.dbname)

    @mcp.tool(title="Use database", annotations=READ_ONLY)
    def use_database(name: str) -> dict:
        """Switch the current target database (same cluster, role `mcp`).

        All subsequent tool calls act on ``name`` until it is switched again. The
        database must be one role `mcp` can `CONNECT` to in this cluster — call
        `list_databases` to discover valid names. Returns the capability report
        for the new database (its DB tier may differ from the previous one). On
        failure the current database is left unchanged.
        """
        try:
            target = ctx.manager.use(name)
        except Exception as exc:  # noqa: BLE001 - surface connect/probe errors
            return {
                "ok": False,
                "error": f"could not switch to database {name!r}: {exc}",
                "database": ctx.manager.current,
            }
        report = target.caps.report(database=target.dbname)
        report["ok"] = True
        return report

    @mcp.tool(title="Health check", annotations=READ_ONLY)
    def health_check() -> dict:
        """Check that the service is up and the current target database is reachable."""
        t = ctx.manager.current_target()
        caps = t.caps
        try:
            notices = caps.guard()
        except CapabilityError as exc:
            notices = exc.notices
        db_ok, version, err = True, None, None
        try:
            version = t.db.query_scalar("SELECT version()")
        except Exception as exc:  # noqa: BLE001
            db_ok, err = False, str(exc)
        return attach(
            {
                "ok": db_ok,
                "service": "mcp-postgres",
                "database_connected": db_ok,
                "server_version": version,
                "error": err,
                "os_tier": caps.os_tier().name,
                "db_tier": caps.db_tier().name if db_ok else None,
            },
            notices,
            database=t.dbname,
        )

    @mcp.tool(title="List databases", annotations=READ_ONLY)
    def list_databases() -> dict:
        """List non-template databases with owner and encoding.

        These are the names accepted by `use_database` (subject to role `mcp`
        having `CONNECT`).
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READONLY, database=t.dbname)
        if not allowed:
            return info
        cols, rows = t.db.select(
            "SELECT datname, pg_get_userbyid(datdba) AS owner, "
            "pg_encoding_to_char(encoding) AS encoding "
            "FROM pg_database WHERE datistemplate = false ORDER BY datname"
        )
        return attach({"ok": True, "databases": rows_as_dicts(cols, rows)}, info, database=t.dbname)

    @mcp.tool(title="List schemas", annotations=READ_ONLY)
    def list_schemas() -> dict:
        """List non-system schemas in the current target database."""
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READONLY, database=t.dbname)
        if not allowed:
            return info
        cols, rows = t.db.select(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT LIKE 'pg_%' AND schema_name <> 'information_schema' "
            "ORDER BY schema_name"
        )
        return attach({"ok": True, "schemas": [r[0] for r in rows]}, info, database=t.dbname)

    @mcp.tool(title="List tables", annotations=READ_ONLY)
    def list_tables(schema: str = "public") -> dict:
        """List tables and views in a schema (default: public) of the current database."""
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READONLY, database=t.dbname)
        if not allowed:
            return info
        cols, rows = t.db.select(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = %s ORDER BY table_name",
            (schema,),
        )
        return attach(
            {"ok": True, "schema": schema, "tables": rows_as_dicts(cols, rows)},
            info,
            database=t.dbname,
        )

    @mcp.tool(title="Describe table", annotations=READ_ONLY)
    def describe_table(table: str, schema: str = "public") -> dict:
        """Describe a table's columns and primary key in the current database."""
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READONLY, database=t.dbname)
        if not allowed:
            return info
        cols, rows = t.db.select(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, table),
        )
        if not rows:
            return attach(
                {"ok": False, "error": f"table {schema}.{table} not found or not visible"},
                info,
                database=t.dbname,
            )
        _pkc, pk_rows = t.db.select(
            "SELECT kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON kcu.constraint_name = tc.constraint_name "
            " AND kcu.table_schema = tc.table_schema "
            "WHERE tc.table_schema = %s AND tc.table_name = %s "
            "  AND tc.constraint_type = 'PRIMARY KEY' "
            "ORDER BY kcu.ordinal_position",
            (schema, table),
        )
        return attach(
            {
                "ok": True,
                "schema": schema,
                "table": table,
                "columns": rows_as_dicts(cols, rows),
                "primary_key": [r[0] for r in pk_rows],
            },
            info,
            database=t.dbname,
        )
