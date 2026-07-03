"""Query tools: read-only (always) and read-write (DB_READWRITE+)."""

from __future__ import annotations

from mcp.types import ToolAnnotations

from ..capabilities import DbTier
from .base import attach, guard_or_error, rows_as_dicts


def register(mcp, ctx) -> None:
    @mcp.tool(
        title="Run read-only query",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    def run_read_query(sql: str, max_rows: int = 1000) -> dict:
        """Run a SELECT (or other read) inside a forced READ ONLY transaction.

        Runs against the current target database (switch it with `use_database`).
        Any statement that attempts to write fails — this tool is safe even when
        the connected role has write privileges.
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READONLY, database=t.dbname)
        if not allowed:
            return info
        try:
            cols, rows, truncated = t.db.run_read_query(sql, max_rows=max(1, min(max_rows, 10000)))
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
