"""Query tools: read-only (always) and read-write (DB_READWRITE+)."""

from __future__ import annotations

from ..capabilities import DbTier
from .base import attach, guard_or_error, rows_as_dicts


def register(mcp, ctx) -> None:
    caps = ctx.caps
    db = ctx.db

    @mcp.tool()
    def run_read_query(sql: str, max_rows: int = 1000) -> dict:
        """Run a SELECT (or other read) inside a forced READ ONLY transaction.

        Any statement that attempts to write fails — this tool is safe even when
        the connected role has write privileges.
        """
        allowed, info = guard_or_error(caps, db_min=DbTier.DB_READONLY)
        if not allowed:
            return info
        try:
            cols, rows, truncated = db.run_read_query(sql, max_rows=max(1, min(max_rows, 10000)))
        except Exception as exc:  # noqa: BLE001 - surface SQL errors to the caller
            return attach({"ok": False, "error": str(exc)}, info)
        return attach(
            {
                "ok": True,
                "columns": cols,
                "row_count": len(rows),
                "truncated": truncated,
                "rows": rows_as_dicts(cols, rows),
            },
            info,
        )

    # Registered unconditionally so a role that gains write access mid-session is
    # still protected by the guard; only enabled in the report when the tier holds.
    @mcp.tool()
    def execute_sql(sql: str) -> dict:
        """Execute a DML/DDL statement. Requires DB tier DB_READWRITE or higher."""
        allowed, info = guard_or_error(caps, db_min=DbTier.DB_READWRITE)
        if not allowed:
            return info
        try:
            result = db.execute(sql)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info)
        result["ok"] = True
        return attach(result, info)

    ctx.enabled_tools.append("run_read_query")
    if caps.db_tier() >= DbTier.DB_READWRITE:
        ctx.enabled_tools.append("execute_sql")
