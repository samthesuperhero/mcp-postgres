"""Observability & ops tools.

Read-only windows into what a live cluster is *doing* — running backends
(``server_activity``), blocking chains (``list_locks``), per-database size/cache
health (``database_stats``), and the effective server configuration
(``get_settings``, straight from ``pg_settings``). All four gate at ``DB_READONLY``
and touch nothing; notably ``get_settings`` gives read-only config visibility
*without* the ``OS_CONFIG`` tier the file-editing tools need.

Two intervention tools — ``cancel_query`` (gentle) and ``terminate_backend``
(forceful) — signal a backend by pid and require ``DB_ADMIN`` (superuser), since
cancelling/terminating another role's backend is a superuser action. Both refuse to
signal the service's own backend.

Every tool follows the house shape: ``current_target()`` → ``guard_or_error`` →
run via ``t.db`` → ``attach(result, info, database=t.dbname)``.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

from ..capabilities import DbTier
from .base import attach, guard_or_error, rows_as_dicts

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

# Clamp for the server_activity page size, so a caller can't ask for an unbounded scan.
_MAX_ACTIVITY_ROWS = 1000


def _like_prefix(value: str) -> str:
    """Build a LIKE pattern matching ``value`` as a LITERAL prefix.

    Escapes LIKE's metacharacters (``\\`` ``%`` ``_``) so e.g. ``get_settings(name="log_")``
    returns settings starting with the literal ``log_`` — not ``log`` followed by any single
    character (which would also match ``logging_collector``). Relies on LIKE's default
    backslash escape, and the value is passed as a bind parameter, not spliced into the SQL.
    """
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def _signal_backend(t, info, pid: int, func: str) -> dict:
    """Run ``pg_cancel_backend``/``pg_terminate_backend`` for ``pid``, refusing self.

    ``func`` is a fixed catalog function name chosen by the caller (never user input),
    so formatting it into the statement is safe. The self-check and the signal run in
    ONE statement (one pool checkout) so ``pg_backend_pid()`` is consistent with the
    call that actually signals.
    """
    try:
        row = t.db.query_one(
            "SELECT %s = pg_backend_pid() AS is_self, "
            "       CASE WHEN %s = pg_backend_pid() THEN NULL "
            f"           ELSE {func}(%s) END AS signalled",
            (pid, pid, pid),
        )
    except Exception as exc:  # noqa: BLE001 - surface a bad pid / privilege error
        return attach({"ok": False, "pid": pid, "error": str(exc)}, info, database=t.dbname)
    if row and row["is_self"]:
        return attach(
            {"ok": False, "pid": pid, "error": "refusing to signal the service's own backend"},
            info,
            database=t.dbname,
        )
    return attach(
        {"ok": True, "pid": pid, "signalled": bool(row["signalled"]) if row else False},
        info,
        database=t.dbname,
    )


def register(mcp, ctx) -> None:
    @mcp.tool(title="Server activity", annotations=READ_ONLY)
    def server_activity(
        include_idle: bool = False, all_databases: bool = False, limit: int = 100
    ) -> dict:
        """List live backends from ``pg_stat_activity`` — what is running right now.

        By default scopes to the current target database and hides fully ``idle``
        sessions; set ``all_databases`` to see the whole cluster and ``include_idle`` to
        keep idle connections. Each row carries pid, user, application, client address,
        state, the wait event (if any), the backend/xact/query start times, how long the
        current query has run (``query_runtime_s``), and the query text — enough to spot a
        runaway or blocked statement and get its pid for `cancel_query`.
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READONLY, database=t.dbname)
        if not allowed:
            return info
        clauses = []
        if not all_databases:
            clauses.append("datname = current_database()")
        if not include_idle:
            clauses.append("state IS DISTINCT FROM 'idle'")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        n = max(1, min(int(limit), _MAX_ACTIVITY_ROWS))
        cols, rows = t.db.select(
            "SELECT pid, usename AS user, datname AS database, application_name, "
            "       client_addr, backend_type, state, wait_event_type, wait_event, "
            "       backend_start, xact_start, query_start, state_change, "
            "       EXTRACT(EPOCH FROM (now() - query_start))::float8 AS query_runtime_s, "
            "       query "
            f"FROM pg_stat_activity {where} "
            "ORDER BY query_start NULLS LAST LIMIT %s",
            [n],
        )
        return attach(
            {"ok": True, "backend_count": len(rows), "backends": rows_as_dicts(cols, rows)},
            info,
            database=t.dbname,
        )

    @mcp.tool(title="List lock waits", annotations=READ_ONLY)
    def list_locks() -> dict:
        """Show blocking chains — which backends are waiting on which (the "why is it hanging" view).

        Uses ``pg_blocking_pids()`` to list every backend currently blocked by another,
        with its query and wait event, the blocking pid(s), and the blocking queries.
        An empty list means nothing is blocked. Feed a blocker pid to `cancel_query` /
        `terminate_backend` (DB_ADMIN) to clear a stuck chain.
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READONLY, database=t.dbname)
        if not allowed:
            return info
        cols, rows = t.db.select(
            "SELECT blocked.pid AS blocked_pid, blocked.usename AS blocked_user, "
            "       blocked.datname AS database, blocked.wait_event_type, blocked.wait_event, "
            "       blocked.query AS blocked_query, "
            "       pg_blocking_pids(blocked.pid) AS blocking_pids, "
            "       blockers.blocking_queries "
            "FROM pg_stat_activity blocked "
            "CROSS JOIN LATERAL ("
            "    SELECT array_agg(b.query) AS blocking_queries "
            "    FROM pg_stat_activity b "
            "    WHERE b.pid = ANY(pg_blocking_pids(blocked.pid))"
            ") blockers "
            "WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0 "
            "ORDER BY blocked.pid"
        )
        return attach(
            {"ok": True, "blocked_count": len(rows), "blocked": rows_as_dicts(cols, rows)},
            info,
            database=t.dbname,
        )

    @mcp.tool(title="Database statistics", annotations=READ_ONLY)
    def database_stats(top_tables: int = 10) -> dict:
        """Report size and activity for the current target database.

        Returns per-DB counters from ``pg_stat_database`` (backends, commits/rollbacks,
        block reads/hits with a computed ``cache_hit_ratio``, tuple counts, deadlocks,
        temp usage), the total on-disk ``size``, and the ``top_tables`` largest tables by
        total relation size. A low cache-hit ratio or a fast-growing table is visible at a
        glance.
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READONLY, database=t.dbname)
        if not allowed:
            return info
        scols, srows = t.db.select(
            "SELECT d.datname AS database, "
            "       pg_database_size(d.oid) AS size_bytes, "
            "       pg_size_pretty(pg_database_size(d.oid)) AS size, "
            "       s.numbackends, s.xact_commit, s.xact_rollback, "
            "       s.blks_read, s.blks_hit, "
            "       CASE WHEN s.blks_read + s.blks_hit > 0 "
            "            THEN (s.blks_hit::float8 / (s.blks_read + s.blks_hit)) "
            "            ELSE NULL END AS cache_hit_ratio, "
            "       s.tup_returned, s.tup_fetched, s.tup_inserted, s.tup_updated, s.tup_deleted, "
            "       s.deadlocks, s.temp_files, s.temp_bytes "
            "FROM pg_stat_database s JOIN pg_database d ON d.oid = s.datid "
            "WHERE d.datname = current_database()"
        )
        stats = rows_as_dicts(scols, srows)
        n = max(1, min(int(top_tables), 100))
        tcols, trows = t.db.select(
            "SELECT n.nspname AS schema, c.relname AS table, "
            "       pg_total_relation_size(c.oid) AS total_bytes, "
            "       pg_size_pretty(pg_total_relation_size(c.oid)) AS size, "
            "       c.reltuples::bigint AS approx_row_count "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p', 'm') "
            "  AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
            "  AND n.nspname NOT LIKE 'pg_toast%%' "
            "ORDER BY pg_total_relation_size(c.oid) DESC LIMIT %s",
            [n],
        )
        return attach(
            {
                "ok": True,
                "stats": stats[0] if stats else None,
                "top_tables": rows_as_dicts(tcols, trows),
            },
            info,
            database=t.dbname,
        )

    @mcp.tool(title="Get settings", annotations=READ_ONLY)
    def get_settings(name: str | None = None, category: str | None = None) -> dict:
        """Read effective server configuration from ``pg_settings`` (no OS tier needed).

        A read-only view of the running configuration — complements the sudo-gated config
        *edit* tools, which an agent may not have. ``name`` matches a setting exactly or by
        prefix (e.g. ``"log_"``); ``category`` filters by category (case-insensitive). With
        no filter every setting is returned. Each row carries the current ``setting`` +
        ``unit``, ``context`` (when a change takes effect), ``source``, whether a restart is
        pending, and a short description.
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_READONLY, database=t.dbname)
        if not allowed:
            return info
        clauses, params = [], []
        if name:
            clauses.append("(name = %s OR name LIKE %s)")
            params += [name, _like_prefix(name)]
        if category:
            clauses.append("category ILIKE %s")
            params.append(category)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cols, rows = t.db.select(
            "SELECT name, setting, unit, category, context, vartype, source, "
            "       boot_val, reset_val, pending_restart, short_desc "
            f"FROM pg_settings {where} ORDER BY category, name",
            params,
        )
        return attach(
            {"ok": True, "setting_count": len(rows), "settings": rows_as_dicts(cols, rows)},
            info,
            database=t.dbname,
        )

    # Registered unconditionally so a role promoted to admin mid-session is usable
    # without a restart; the guard enforces DB_ADMIN on every call.
    @mcp.tool(
        title="Cancel a running query",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    def cancel_query(pid: int) -> dict:
        """Cancel the statement a backend is running (``pg_cancel_backend``). Requires DB_ADMIN.

        The gentle option: it cancels the *current query* but leaves the connection open.
        Get the ``pid`` from `server_activity` or `list_locks`. Returns ``signalled`` —
        false if the backend was already gone. Refuses to signal the service's own backend.
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_ADMIN, database=t.dbname)
        if not allowed:
            return info
        return _signal_backend(t, info, int(pid), "pg_cancel_backend")

    @mcp.tool(
        title="Terminate a backend",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, openWorldHint=False
        ),
    )
    def terminate_backend(pid: int) -> dict:
        """Terminate a backend connection (``pg_terminate_backend``). Requires DB_ADMIN.

        The forceful option: it closes the whole connection, rolling back its transaction —
        use it when `cancel_query` won't free a stuck backend. Get the ``pid`` from
        `server_activity` / `list_locks`. Returns ``signalled`` (false if already gone) and
        refuses to signal the service's own backend.
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_ADMIN, database=t.dbname)
        if not allowed:
            return info
        return _signal_backend(t, info, int(pid), "pg_terminate_backend")
