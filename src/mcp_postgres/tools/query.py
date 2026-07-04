"""Query tools: read-only (always) and read-write (DB_READWRITE+)."""

from __future__ import annotations

from mcp.types import ToolAnnotations
from psycopg import sql

from ..capabilities import DbTier
from .base import attach, guard_or_error, rows_as_dicts

# Upper bound on statement_timeout for the read path (10 minutes), so a caller can't
# ask for an effectively-unbounded read that pins a pool connection indefinitely.
_MAX_TIMEOUT_MS = 600_000


def register(mcp, ctx) -> None:
    @mcp.tool(
        title="Run read-only query",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    def run_read_query(sql: str, max_rows: int = 1000, timeout_ms: int = 30000) -> dict:
        """Run a SELECT (or other read) inside a forced READ ONLY transaction.

        Runs against the current target database (switch it with `use_database`).
        Any statement that attempts to write fails — this tool is safe even when
        the connected role has write privileges. ``timeout_ms`` (default 30s, capped
        at 10min) bounds the query via ``statement_timeout`` so a runaway read cannot
        pin a connection; pass ``0`` to disable the bound.
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READONLY, database=t.dbname)
        if not allowed:
            return info
        timeout = None if timeout_ms in (0, None) else max(1, min(int(timeout_ms), _MAX_TIMEOUT_MS))
        try:
            cols, rows, truncated = t.db.run_read_query(
                sql, max_rows=max(1, min(max_rows, 10000)), timeout_ms=timeout
            )
        except Exception as exc:  # noqa: BLE001 - surface SQL errors to the caller
            return attach({"ok": False, "error": str(exc)}, info, database=t.dbname)
        return attach(
            {
                "ok": True,
                "columns": cols,
                "row_count": len(rows),
                "truncated": truncated,
                "rows": rows_as_dicts(cols, rows),
            },
            info,
            database=t.dbname,
        )

    @mcp.tool(
        title="Explain query",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    def explain_query(sql: str, analyze: bool = False, format: str = "text",
                      timeout_ms: int = 30000) -> dict:
        """Return the PostgreSQL plan for a query, to reason about performance.

        Runs ``EXPLAIN`` inside a forced READ ONLY transaction that is always rolled
        back. With ``analyze=True`` the query is actually executed to gather real row
        counts and timings, but — because the transaction is READ ONLY and rolled back
        — any write is rejected and all side effects are discarded. ``format`` is
        ``text`` (human-readable) or ``json`` (structured plan tree).
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READONLY, database=t.dbname)
        if not allowed:
            return info
        timeout = None if timeout_ms in (0, None) else max(1, min(int(timeout_ms), _MAX_TIMEOUT_MS))
        try:
            fmt, plan = t.db.explain(sql, analyze=analyze, fmt=format, timeout_ms=timeout)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info, database=t.dbname)
        return attach(
            {"ok": True, "analyze": analyze, "format": fmt, "plan": plan},
            info,
            database=t.dbname,
        )

    @mcp.tool(
        title="Sample table rows",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    def sample_table(table: str, schema: str = "public", limit: int = 20) -> dict:
        """Return the first N rows of a table/view — a quick preview.

        Convenience wrapper over the READ ONLY path with safe identifier quoting, so
        an agent need not hand-write ``SELECT * FROM … LIMIT``.
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READONLY, database=t.dbname)
        if not allowed:
            return info
        n = max(1, min(int(limit), 1000))
        stmt = sql.SQL("SELECT * FROM {}.{} LIMIT {}").format(
            sql.Identifier(schema), sql.Identifier(table), sql.Literal(n)
        )
        try:
            cols, rows, truncated = t.db.run_read_query(stmt, max_rows=n, timeout_ms=30000)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info, database=t.dbname)
        return attach(
            {
                "ok": True,
                "schema": schema,
                "table": table,
                "row_count": len(rows),
                "truncated": truncated,
                "rows": rows_as_dicts(cols, rows),
            },
            info,
            database=t.dbname,
        )

    # Registered unconditionally so a role that gains write access mid-session is
    # still protected by the guard; only enabled in the report when the tier holds.
    @mcp.tool(
        title="Execute SQL (DML/DDL)",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, openWorldHint=False
        ),
    )
    def execute_sql(sql: str) -> dict:
        """Execute a DML/DDL statement on the current target database.

        Requires DB tier DB_READWRITE or higher (switch DBs with `use_database`).
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READWRITE, database=t.dbname)
        if not allowed:
            return info
        try:
            result = t.db.execute(sql)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info, database=t.dbname)
        result["ok"] = True
        return attach(result, info, database=t.dbname)

    @mcp.tool(
        title="Execute SQL batch (transaction)",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, openWorldHint=False
        ),
    )
    def execute_batch(statements: list[str], stop_on_error: bool = True) -> dict:
        """Execute several statements in ONE transaction (atomic by default).

        Requires DB tier DB_READWRITE or higher. With ``stop_on_error`` (default) the
        first failing statement rolls back the whole batch and NOTHING is applied — use
        this for multi-step migrations that must be all-or-nothing. With
        ``stop_on_error=False`` each failing statement is skipped (rolled back to a
        savepoint) and the successful ones are committed together. The result reports
        ``committed``, a per-statement ``results`` list, and the first ``failed_index``.
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READWRITE, database=t.dbname)
        if not allowed:
            return info
        if not statements:
            return attach({"ok": False, "error": "no statements provided"}, info, database=t.dbname)
        try:
            result = t.db.execute_batch(list(statements), stop_on_error=stop_on_error)
        except Exception as exc:  # noqa: BLE001 - transaction machinery failure
            return attach({"ok": False, "error": str(exc)}, info, database=t.dbname)
        result["ok"] = result.get("committed", False)
        return attach(result, info, database=t.dbname)
