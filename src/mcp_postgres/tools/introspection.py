"""Always-available tools: capability report, health, and read-only introspection."""

from __future__ import annotations

from ..capabilities import CapabilityError, DbTier
from .base import attach, guard_or_error, rows_as_dicts


def register(mcp, ctx) -> None:
    caps = ctx.caps
    db = ctx.db

    @mcp.tool()
    def get_capabilities() -> dict:
        """Report the server's current OS and DB privilege tiers and enabled tools."""
        return caps.report(ctx.enabled_tools)

    @mcp.tool()
    def health_check() -> dict:
        """Check that the service is up and PostgreSQL is reachable."""
        try:
            notices = caps.guard()
        except CapabilityError as exc:
            notices = exc.notices
        db_ok, version, err = True, None, None
        try:
            version = db.query_scalar("SELECT version()")
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
        )

    @mcp.tool()
    def list_databases() -> dict:
        """List non-template databases with owner and encoding."""
        allowed, info = guard_or_error(caps, db_min=DbTier.DB_READONLY)
        if not allowed:
            return info
        cols, rows = db.select(
            "SELECT datname, pg_get_userbyid(datdba) AS owner, "
            "pg_encoding_to_char(encoding) AS encoding "
            "FROM pg_database WHERE datistemplate = false ORDER BY datname"
        )
        return attach({"ok": True, "databases": rows_as_dicts(cols, rows)}, info)

    @mcp.tool()
    def list_schemas() -> dict:
        """List non-system schemas in the current database."""
        allowed, info = guard_or_error(caps, db_min=DbTier.DB_READONLY)
        if not allowed:
            return info
        cols, rows = db.select(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT LIKE 'pg_%' AND schema_name <> 'information_schema' "
            "ORDER BY schema_name"
        )
        return attach({"ok": True, "schemas": [r[0] for r in rows]}, info)

    @mcp.tool()
    def list_tables(schema: str = "public") -> dict:
        """List tables and views in a schema (default: public)."""
        allowed, info = guard_or_error(caps, db_min=DbTier.DB_READONLY)
        if not allowed:
            return info
        cols, rows = db.select(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = %s ORDER BY table_name",
            (schema,),
        )
        return attach({"ok": True, "schema": schema, "tables": rows_as_dicts(cols, rows)}, info)

    @mcp.tool()
    def describe_table(table: str, schema: str = "public") -> dict:
        """Describe a table's columns and primary key."""
        allowed, info = guard_or_error(caps, db_min=DbTier.DB_READONLY)
        if not allowed:
            return info
        cols, rows = db.select(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, table),
        )
        if not rows:
            return attach(
                {"ok": False, "error": f"table {schema}.{table} not found or not visible"},
                info,
            )
        _pkc, pk_rows = db.select(
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
        )

    ctx.enabled_tools += [
        "get_capabilities",
        "health_check",
        "list_databases",
        "list_schemas",
        "list_tables",
        "describe_table",
    ]
