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

    def run_read_query(self, sql, params=None, max_rows: int = 1000):
        """Run a query inside a READ ONLY transaction. Returns (cols, rows, truncated)."""
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            try:
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
