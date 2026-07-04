"""Schema-wide introspection tools (read-only, always available at DB_READONLY).

These complement the per-table `describe_table` (introspection.py) with schema-level
views an agent needs to understand a database before writing SQL: the foreign-key
relationship map, indexes, views, functions, enum types, and on-demand object DDL.
All run against the current target database and only read.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

from ..capabilities import DbTier
from .base import attach, guard_or_error, rows_as_dicts

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

# get_object_definition: the object kinds we can render DDL for, and how.
_DEF_RELKINDS = {"view": ("v",), "materialized_view": ("m",), "index": ("i",)}


def register(mcp, ctx) -> None:
    def _read_guard(t):
        return guard_or_error(t.caps, db_min=DbTier.DB_READONLY, database=t.dbname)

    @mcp.tool(title="List foreign keys", annotations=_READ_ONLY)
    def list_foreign_keys(schema: str = "public") -> dict:
        """List every foreign-key relationship in a schema (the JOIN map).

        One call returns all FK edges (from table.columns → referenced table.columns)
        so an agent can see how the tables relate without inspecting each one.
        """
        t = ctx.manager.current_target()
        allowed, info = _read_guard(t)
        if not allowed:
            return info
        cols, rows = t.db.select(
            "SELECT t.relname AS table, con.conname AS name, "
            "       ARRAY(SELECT att.attname FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "             JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum "
            "             ORDER BY k.ord) AS columns, "
            "       fn.nspname AS foreign_schema, ft.relname AS foreign_table, "
            "       ARRAY(SELECT att.attname FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord) "
            "             JOIN pg_attribute att ON att.attrelid = con.confrelid AND att.attnum = k.attnum "
            "             ORDER BY k.ord) AS foreign_columns, "
            "       pg_get_constraintdef(con.oid) AS definition "
            "FROM pg_constraint con "
            "JOIN pg_class t ON t.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN pg_class ft ON ft.oid = con.confrelid "
            "JOIN pg_namespace fn ON fn.oid = ft.relnamespace "
            "WHERE con.contype = 'f' AND n.nspname = %s "
            "ORDER BY t.relname, con.conname",
            (schema,),
        )
        return attach(
            {"ok": True, "schema": schema, "foreign_keys": rows_as_dicts(cols, rows)},
            info,
            database=t.dbname,
        )

    @mcp.tool(title="List indexes", annotations=_READ_ONLY)
    def list_indexes(schema: str = "public", table: str | None = None) -> dict:
        """List indexes in a schema, or on one table when ``table`` is given."""
        t = ctx.manager.current_target()
        allowed, info = _read_guard(t)
        if not allowed:
            return info
        query = (
            "SELECT t.relname AS table, i.relname AS name, ix.indisunique AS is_unique, "
            "       ix.indisprimary AS is_primary, am.amname AS method, "
            "       ARRAY(SELECT pg_get_indexdef(ix.indexrelid, k + 1, true) "
            "             FROM generate_subscripts(ix.indkey, 1) AS k ORDER BY k) AS columns, "
            "       pg_get_indexdef(ix.indexrelid) AS definition, "
            "       pg_size_pretty(pg_relation_size(ix.indexrelid)) AS size "
            "FROM pg_index ix "
            "JOIN pg_class i ON i.oid = ix.indexrelid "
            "JOIN pg_class t ON t.oid = ix.indrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN pg_am am ON am.oid = i.relam "
            "WHERE n.nspname = %s"
        )
        params: tuple = (schema,)
        if table is not None:
            query += " AND t.relname = %s"
            params = (schema, table)
        query += " ORDER BY t.relname, i.relname"
        cols, rows = t.db.select(query, params)
        return attach(
            {"ok": True, "schema": schema, "table": table, "indexes": rows_as_dicts(cols, rows)},
            info,
            database=t.dbname,
        )

    @mcp.tool(title="List views", annotations=_READ_ONLY)
    def list_views(schema: str = "public", include_definition: bool = False) -> dict:
        """List views and materialized views in a schema.

        With ``include_definition`` each entry also carries its ``SELECT`` body
        (``pg_get_viewdef``).
        """
        t = ctx.manager.current_target()
        allowed, info = _read_guard(t)
        if not allowed:
            return info
        defn = ", pg_get_viewdef(c.oid, true) AS definition" if include_definition else ""
        cols, rows = t.db.select(
            "SELECT c.relname AS name, "
            "       CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' END AS kind, "
            "       obj_description(c.oid) AS comment" + defn + " "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('v', 'm') ORDER BY c.relname",
            (schema,),
        )
        return attach(
            {"ok": True, "schema": schema, "views": rows_as_dicts(cols, rows)},
            info,
            database=t.dbname,
        )

    @mcp.tool(title="List functions", annotations=_READ_ONLY)
    def list_functions(schema: str = "public") -> dict:
        """List functions and procedures in a schema (signature, return type, language)."""
        t = ctx.manager.current_target()
        allowed, info = _read_guard(t)
        if not allowed:
            return info
        cols, rows = t.db.select(
            "SELECT p.proname AS name, "
            "       pg_get_function_arguments(p.oid) AS arguments, "
            "       pg_get_function_result(p.oid) AS returns, "
            "       l.lanname AS language, "
            "       CASE p.prokind WHEN 'f' THEN 'function' WHEN 'p' THEN 'procedure' END AS kind "
            "FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language l ON l.oid = p.prolang "
            "WHERE n.nspname = %s AND p.prokind IN ('f', 'p') ORDER BY p.proname",
            (schema,),
        )
        return attach(
            {"ok": True, "schema": schema, "functions": rows_as_dicts(cols, rows)},
            info,
            database=t.dbname,
        )

    @mcp.tool(title="List enum types", annotations=_READ_ONLY)
    def list_enums(schema: str = "public") -> dict:
        """List enum types in a schema with their labels, in sort order.

        Agents frequently need an enum's valid values before writing an INSERT/UPDATE.
        """
        t = ctx.manager.current_target()
        allowed, info = _read_guard(t)
        if not allowed:
            return info
        cols, rows = t.db.select(
            "SELECT ty.typname AS name, "
            "       ARRAY(SELECT e.enumlabel FROM pg_enum e "
            "             WHERE e.enumtypid = ty.oid ORDER BY e.enumsortorder) AS labels "
            "FROM pg_type ty JOIN pg_namespace n ON n.oid = ty.typnamespace "
            "WHERE n.nspname = %s AND ty.typtype = 'e' ORDER BY ty.typname",
            (schema,),
        )
        return attach(
            {"ok": True, "schema": schema, "enums": rows_as_dicts(cols, rows)},
            info,
            database=t.dbname,
        )

    @mcp.tool(title="Get object definition", annotations=_READ_ONLY)
    def get_object_definition(kind: str, name: str, schema: str = "public") -> dict:
        """Return the DDL/definition of a database object.

        ``kind`` is one of ``view``, ``materialized_view``, ``index``, or ``function``.
        For an overloaded function pass a full signature as ``name`` (e.g.
        ``my_fn(integer, text)``); a bare name works when it is unambiguous.
        """
        t = ctx.manager.current_target()
        allowed, info = _read_guard(t)
        if not allowed:
            return info

        kind = kind.lower()
        try:
            if kind == "function":
                definition = _function_def(t.db, schema, name)
            elif kind in _DEF_RELKINDS:
                definition = _relation_def(t.db, schema, name, kind)
            else:
                return attach(
                    {"ok": False, "error": f"unsupported kind {kind!r}; expected one of "
                                           "view, materialized_view, index, function"},
                    info, database=t.dbname,
                )
        except LookupError as exc:
            return attach({"ok": False, "error": str(exc)}, info, database=t.dbname)
        except Exception as exc:  # noqa: BLE001 - surface SQL errors
            return attach({"ok": False, "error": str(exc)}, info, database=t.dbname)

        return attach(
            {"ok": True, "schema": schema, "kind": kind, "name": name, "definition": definition},
            info,
            database=t.dbname,
        )


def _relation_def(db, schema: str, name: str, kind: str) -> str:
    """DDL for a view/matview/index; raises LookupError if it doesn't exist/match kind."""
    row = db.query_one(
        "SELECT c.oid, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        (schema, name),
    )
    if not row:
        raise LookupError(f"{kind} {schema}.{name} not found or not visible")
    if row["relkind"] not in _DEF_RELKINDS[kind]:
        raise LookupError(f"{schema}.{name} is not a {kind}")
    oid = row["oid"]
    if kind == "index":
        return db.query_scalar("SELECT pg_get_indexdef(%s)", (oid,))
    return db.query_scalar("SELECT pg_get_viewdef(%s, true)", (oid,))


def _function_def(db, schema: str, name: str) -> str:
    """DDL for a function; disambiguates overloads or lists candidates."""
    if "(" in name:
        # quote_ident handles the schema; the signature (e.g. "fn(int, text)") is
        # appended verbatim. Concatenation avoids format()'s %-specifiers colliding
        # with psycopg's own %s placeholders.
        oid = db.query_scalar(
            "SELECT to_regprocedure(quote_ident(%s) || '.' || %s)::oid", (schema, name)
        )
        if oid is None:
            raise LookupError(f"function {schema}.{name} not found or not visible")
        return db.query_scalar("SELECT pg_get_functiondef(%s)", (oid,))
    _c, rows = db.select(
        "SELECT p.oid, pg_get_function_arguments(p.oid) AS args FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = %s AND p.proname = %s AND p.prokind IN ('f', 'p')",
        (schema, name),
    )
    if not rows:
        raise LookupError(f"function {schema}.{name} not found or not visible")
    if len(rows) > 1:
        sigs = ", ".join(f"{name}({args})" for _oid, args in rows)
        raise LookupError(f"function {schema}.{name} is overloaded; pass a full signature. "
                          f"Candidates: {sigs}")
    return db.query_scalar("SELECT pg_get_functiondef(%s)", (rows[0][0],))
