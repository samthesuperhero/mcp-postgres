"""PostgreSQL access layer.

A small wrapper over a psycopg (v3) connection pool. All connections are opened
in autocommit mode; read-only user queries are run inside an explicit
``BEGIN READ ONLY`` transaction so they are safe even when the role can write.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from .config import DatabaseConfig

log = logging.getLogger(__name__)


class Database:
    def __init__(self, dbcfg: DatabaseConfig):
        self.cfg = dbcfg
        conninfo = make_conninfo(
            host=dbcfg.host,
            port=dbcfg.port,
            user=dbcfg.user,
            password=dbcfg.password,
            dbname=dbcfg.dbname,
        )
        # open=False: the service must start even if PostgreSQL is momentarily down.
        self.pool = ConnectionPool(
            conninfo,
            min_size=0,
            max_size=5,
            open=False,
            timeout=10,
            kwargs={"autocommit": True, "connect_timeout": 5},
        )

    def open(self) -> None:
        self.pool.open()

    def close(self) -> None:
        try:
            self.pool.close()
        except Exception:  # noqa: BLE001 - shutdown best-effort
            pass

    # -- internal helpers (trusted, parameterised queries) --------------------

    def select(self, sql, params=None):
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in cur.description] if cur.description else []
            rows = cur.fetchall() if cur.description else []
            return cols, rows

    def query_one(self, sql, params=None) -> dict | None:
        cols, rows = self.select(sql, params)
        return dict(zip(cols, rows[0])) if rows else None

    def query_scalar(self, sql, params=None):
        _cols, rows = self.select(sql, params)
        return rows[0][0] if rows else None

    # -- user-facing query paths ---------------------------------------------

    def run_read_query(self, sql, params=None, max_rows: int = 1000, timeout_ms: int | None = None):
        """Run a query inside a READ ONLY transaction. Returns (cols, rows, truncated).

        When ``timeout_ms`` is set, a per-transaction ``statement_timeout`` bounds the
        query so a runaway read cannot pin a pool connection; on expiry PostgreSQL
        raises ``QueryCanceled`` (surfaced to the caller as an error).
        """
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            try:
                if timeout_ms is not None:
                    cur.execute("SET LOCAL statement_timeout = %s", (int(timeout_ms),))
                cur.execute(sql, params)
                if cur.description:
                    cols = [d.name for d in cur.description]
                    rows = cur.fetchmany(max_rows)
                    truncated = len(rows) == max_rows and cur.fetchone() is not None
                else:
                    cols, rows, truncated = [], [], False
            finally:
                cur.execute("ROLLBACK")
        return cols, rows, truncated

    def explain(self, sql, params=None, analyze: bool = False, fmt: str = "text",
                timeout_ms: int | None = None):
        """Return the plan for ``sql`` from a rolled-back READ ONLY transaction.

        Wrapping ``EXPLAIN`` in ``BEGIN READ ONLY`` … ``ROLLBACK`` means ``ANALYZE``
        (which really executes the statement) is safe: any write is rejected by the
        read-only transaction and every side effect is discarded on rollback.

        Returns ``(fmt, plan)`` where ``plan`` is the parsed JSON object for
        ``fmt='json'`` or the joined plan text otherwise.
        """
        opts = []
        if analyze:
            opts.append("ANALYZE true")
        fmt = (fmt or "text").lower()
        if fmt not in ("text", "json"):
            raise ValueError("format must be 'text' or 'json'")
        opts.append(f"FORMAT {'JSON' if fmt == 'json' else 'TEXT'}")
        prefixed = f"EXPLAIN ({', '.join(opts)}) {sql}"
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            try:
                if timeout_ms is not None:
                    cur.execute("SET LOCAL statement_timeout = %s", (int(timeout_ms),))
                cur.execute(prefixed, params)
                rows = cur.fetchall()
            finally:
                cur.execute("ROLLBACK")
        if fmt == "json":
            # EXPLAIN (FORMAT JSON) returns a single row whose one column is the plan.
            return "json", rows[0][0] if rows else None
        return "text", "\n".join(r[0] for r in rows)

    def execute(self, sql, params=None) -> dict:
        """Execute a statement (autocommit). Returns rowcount/status and any result rows."""
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            out: dict = {"rowcount": cur.rowcount, "status": cur.statusmessage}
            if cur.description:
                cols = [d.name for d in cur.description]
                out["columns"] = cols
                out["rows"] = [list(r) for r in cur.fetchall()]
            return out

    def execute_batch(self, statements: list[str], stop_on_error: bool = True) -> dict:
        """Run several statements in ONE transaction; commit on success, else roll back.

        Connections are autocommit, so an explicit ``BEGIN`` opens the transaction and
        ``COMMIT``/``ROLLBACK`` closes it (mirroring the read path). Each statement runs
        inside a SAVEPOINT so a failure can be handled cleanly (a bare error would
        otherwise poison the rest of the transaction).

        * ``stop_on_error=True`` (default) — the first failing statement aborts the whole
          batch and **nothing is applied**: the atomic-migration guarantee ``execute_sql``
          cannot give.
        * ``stop_on_error=False`` — best effort: a failed statement is rolled back to its
          savepoint (so it does not apply) and the rest still run; the successful ones are
          committed together.

        Returns a dict with ``committed`` and a per-statement ``results`` list; on failure
        it also carries ``failed_index`` and ``error`` (the first failure).
        """
        results: list[dict] = []
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("BEGIN")
            first_failed_index, first_error, aborted = None, None, False
            for i, stmt in enumerate(statements):
                cur.execute("SAVEPOINT mcp_batch")
                try:
                    cur.execute(stmt)
                except Exception as exc:  # noqa: BLE001 - report which statement failed
                    cur.execute("ROLLBACK TO SAVEPOINT mcp_batch")
                    results.append({"ok": False, "error": str(exc)})
                    if first_failed_index is None:
                        first_failed_index, first_error = i, str(exc)
                    if stop_on_error:
                        aborted = True
                        break
                else:
                    cur.execute("RELEASE SAVEPOINT mcp_batch")
                    results.append({"ok": True, "rowcount": cur.rowcount, "status": cur.statusmessage})
            committed = not aborted
            cur.execute("COMMIT" if committed else "ROLLBACK")
        out: dict = {"committed": committed, "statement_count": len(statements), "results": results}
        if first_failed_index is not None:
            out["failed_index"] = first_failed_index
            out["error"] = first_error
        return out
