"""Always-available tools: capability report, health, and read-only introspection."""

from __future__ import annotations

from mcp.types import ToolAnnotations

from ..capabilities import CapabilityError, DbTier
from .base import attach, guard_or_error, rows_as_dicts

# All tools in this module only read; none touch anything outside the local DB.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

# pg_constraint stores referential actions as single-char codes.
_FK_ACTIONS = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}

# pg_class.relkind codes for the relations describe_table accepts.
_RELKIND = {
    "r": "table",
    "p": "partitioned table",
    "v": "view",
    "m": "materialized view",
    "f": "foreign table",
}


def _foreign_keys(db, oid) -> list[dict]:
    """Outbound foreign keys of the relation ``oid`` (columns → referenced table)."""
    _c, rows = db.select(
        "SELECT con.conname, "
        "       ARRAY(SELECT att.attname FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) "
        "             JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum "
        "             ORDER BY k.ord) AS columns, "
        "       fn.nspname AS foreign_schema, ft.relname AS foreign_table, "
        "       ARRAY(SELECT att.attname FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord) "
        "             JOIN pg_attribute att ON att.attrelid = con.confrelid AND att.attnum = k.attnum "
        "             ORDER BY k.ord) AS foreign_columns, "
        "       con.confupdtype AS on_update, con.confdeltype AS on_delete, "
        "       pg_get_constraintdef(con.oid) AS definition "
        "FROM pg_constraint con "
        "JOIN pg_class ft ON ft.oid = con.confrelid "
        "JOIN pg_namespace fn ON fn.oid = ft.relnamespace "
        "WHERE con.contype = 'f' AND con.conrelid = %s ORDER BY con.conname",
        (oid,),
    )
    out = []
    for name, columns, fschema, ftable, fcolumns, on_update, on_delete, definition in rows:
        out.append({
            "name": name,
            "columns": list(columns),
            "foreign_schema": fschema,
            "foreign_table": ftable,
            "foreign_columns": list(fcolumns),
            "on_update": _FK_ACTIONS.get(on_update, on_update),
            "on_delete": _FK_ACTIONS.get(on_delete, on_delete),
            "definition": definition,
        })
    return out


def _referenced_by(db, oid) -> list[dict]:
    """Inbound foreign keys: other tables whose FKs point at the relation ``oid``."""
    _c, rows = db.select(
        "SELECT n.nspname AS schema, t.relname AS table, con.conname AS name, "
        "       pg_get_constraintdef(con.oid) AS definition "
        "FROM pg_constraint con "
        "JOIN pg_class t ON t.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE con.contype = 'f' AND con.confrelid = %s "
        "ORDER BY n.nspname, t.relname, con.conname",
        (oid,),
    )
    return [
        {"schema": schema, "table": table, "name": name, "definition": definition}
        for schema, table, name, definition in rows
    ]


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
        """Describe a table (or view) in the current database.

        Returns columns (type, nullability, default, identity, length/precision,
        comment), the primary key, indexes, outbound foreign keys and inbound
        references (`referenced_by`), unique/check constraints, the table comment,
        and an approximate row count and total size — everything an agent needs to
        write correct SQL against it.
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READONLY, database=t.dbname)
        if not allowed:
            return info

        # Resolve the relation once: existence check + oid used by the catalog queries.
        meta = t.db.query_one(
            "SELECT c.oid, obj_description(c.oid) AS table_comment, c.relkind, "
            "       c.reltuples::bigint AS approx_row_count, "
            "       pg_total_relation_size(c.oid) AS total_bytes, "
            "       pg_size_pretty(pg_total_relation_size(c.oid)) AS size_pretty "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s "
            "  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')",
            (schema, table),
        )
        if not meta:
            return attach(
                {"ok": False, "error": f"table {schema}.{table} not found or not visible"},
                info,
                database=t.dbname,
            )
        oid = meta["oid"]

        col_cols, col_rows = t.db.select(
            "SELECT column_name, data_type, is_nullable, column_default, "
            "       character_maximum_length, numeric_precision, numeric_scale, is_identity "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, table),
        )
        columns = rows_as_dicts(col_cols, col_rows)
        # Column comments aren't in information_schema; merge them in by name.
        comments = {
            name: comment
            for name, comment in t.db.select(
                "SELECT a.attname, col_description(a.attrelid, a.attnum) "
                "FROM pg_attribute a "
                "WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped",
                (oid,),
            )[1]
        }
        for col in columns:
            col["comment"] = comments.get(col["column_name"])

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

        idx_cols, idx_rows = t.db.select(
            "SELECT i.relname AS name, ix.indisunique AS is_unique, "
            "       ix.indisprimary AS is_primary, am.amname AS method, "
            "       pg_get_indexdef(ix.indexrelid) AS definition, "
            "       ARRAY(SELECT pg_get_indexdef(ix.indexrelid, k + 1, true) "
            "             FROM generate_subscripts(ix.indkey, 1) AS k ORDER BY k) AS columns, "
            "       pg_size_pretty(pg_relation_size(ix.indexrelid)) AS size "
            "FROM pg_index ix "
            "JOIN pg_class i ON i.oid = ix.indexrelid "
            "JOIN pg_am am ON am.oid = i.relam "
            "WHERE ix.indrelid = %s ORDER BY i.relname",
            (oid,),
        )

        fkeys = _foreign_keys(t.db, oid)
        referenced_by = _referenced_by(t.db, oid)

        _cc, con_rows = t.db.select(
            "SELECT con.contype, con.conname, pg_get_constraintdef(con.oid) AS definition "
            "FROM pg_constraint con WHERE con.conrelid = %s AND con.contype IN ('u', 'c') "
            "ORDER BY con.contype, con.conname",
            (oid,),
        )
        unique_constraints = [
            {"name": name, "definition": definition}
            for contype, name, definition in con_rows
            if contype == "u"
        ]
        check_constraints = [
            {"name": name, "definition": definition}
            for contype, name, definition in con_rows
            if contype == "c"
        ]

        return attach(
            {
                "ok": True,
                "schema": schema,
                "table": table,
                "kind": _RELKIND.get(meta["relkind"], meta["relkind"]),
                "table_comment": meta["table_comment"],
                "approx_row_count": meta["approx_row_count"],
                "total_bytes": meta["total_bytes"],
                "size": meta["size_pretty"],
                "columns": columns,
                "primary_key": [r[0] for r in pk_rows],
                "indexes": rows_as_dicts(idx_cols, idx_rows),
                "foreign_keys": fkeys,
                "referenced_by": referenced_by,
                "unique_constraints": unique_constraints,
                "check_constraints": check_constraints,
            },
            info,
            database=t.dbname,
        )
