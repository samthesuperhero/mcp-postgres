"""Offline tests for the read/write db-layer helpers behind the query tools.

A fake psycopg connection/cursor drives ``Database.run_read_query``, ``.explain`` and
``.execute_batch`` so the transaction/savepoint control flow is verified without a live
PostgreSQL — the MCP tools in ``tools/query.py`` are thin wrappers over these. Also
covers ``cell()``'s recursive coercion.
"""

from collections import namedtuple
from datetime import date

import pytest

from mcp_postgres.db import Database
from mcp_postgres.tools.base import cell

Col = namedtuple("Col", "name")

# Statements the batch/read machinery issues itself — never made to fail by a test.
_CONTROL = {
    "BEGIN",
    "BEGIN READ ONLY",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT mcp_batch",
    "RELEASE SAVEPOINT mcp_batch",
    "ROLLBACK TO SAVEPOINT mcp_batch",
}


class FakeCursor:
    def __init__(self, fail_on=None, fetch=None, description_for=None):
        self.fail_on = set(fail_on or ())
        self.fetch = fetch or {}
        self.description_for = description_for or (lambda s: None)
        self.executed = []
        self.description = None
        self.rowcount = 1
        self.statusmessage = "OK"
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = sql if isinstance(sql, str) else str(sql)
        self.executed.append((s, params))
        if s not in _CONTROL and not s.startswith("SET LOCAL") and s in self.fail_on:
            raise RuntimeError(f"boom: {s}")
        self.description = self.description_for(s)
        self._rows = self.fetch.get(s, [])

    def fetchall(self):
        return self._rows

    def fetchmany(self, n):
        return self._rows[:n]

    def fetchone(self):
        return None


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self._cursor


class FakePool:
    def __init__(self, cursor):
        self._conn = FakeConn(cursor)

    def connection(self):
        return self._conn


def _db(cursor):
    db = Database.__new__(Database)  # bypass __init__: no real pool needed
    db.pool = FakePool(cursor)
    return db


def _sqls(cursor):
    return [s for s, _ in cursor.executed]


# -- execute_batch -------------------------------------------------------------


def test_execute_batch_atomic_rollback_on_error():
    cur = FakeCursor(fail_on={"BADSQL"})
    res = _db(cur).execute_batch(["CREATE TABLE t(id int)", "INSERT INTO t VALUES(1)", "BADSQL"])
    assert res["committed"] is False
    assert res["failed_index"] == 2
    assert [r["ok"] for r in res["results"]] == [True, True, False]
    sqls = _sqls(cur)
    assert sqls[0] == "BEGIN"
    assert sqls[-1] == "ROLLBACK"  # nothing applied
    assert "ROLLBACK TO SAVEPOINT mcp_batch" in sqls


def test_execute_batch_best_effort_commits_successes():
    cur = FakeCursor(fail_on={"BADSQL"})
    res = _db(cur).execute_batch(["INSERT 1", "BADSQL", "INSERT 2"], stop_on_error=False)
    assert res["committed"] is True
    assert res["failed_index"] == 1  # first failure reported
    assert [r["ok"] for r in res["results"]] == [True, False, True]
    assert _sqls(cur)[-1] == "COMMIT"


def test_execute_batch_all_ok_commits():
    cur = FakeCursor()
    res = _db(cur).execute_batch(["INSERT 1", "INSERT 2"])
    assert res["committed"] is True
    assert "failed_index" not in res
    assert _sqls(cur)[-1] == "COMMIT"


# -- explain -------------------------------------------------------------------


def test_explain_text_wraps_in_readonly_and_rolls_back():
    stmt = "EXPLAIN (FORMAT TEXT) SELECT 1"
    cur = FakeCursor(fetch={stmt: [("Result  (cost=0.00..0.01 rows=1)",), ("  detail",)]})
    fmt, plan = _db(cur).explain("SELECT 1")
    assert fmt == "text"
    assert plan.startswith("Result") and "detail" in plan
    sqls = _sqls(cur)
    assert sqls[0] == "BEGIN READ ONLY"
    assert stmt in sqls
    assert sqls[-1] == "ROLLBACK"  # never commits, even with ANALYZE


def test_explain_analyze_json_returns_parsed_plan():
    stmt = "EXPLAIN (ANALYZE true, FORMAT JSON) SELECT 1"
    plan_obj = [{"Plan": {"Node Type": "Result"}}]
    cur = FakeCursor(fetch={stmt: [(plan_obj,)]})
    fmt, plan = _db(cur).explain("SELECT 1", analyze=True, fmt="json")
    assert fmt == "json"
    assert plan == plan_obj
    assert _sqls(cur)[-1] == "ROLLBACK"


def test_explain_rejects_bad_format():
    with pytest.raises(ValueError):
        _db(FakeCursor()).explain("SELECT 1", fmt="xml")


# -- run_read_query timeout ----------------------------------------------------


def test_run_read_query_sets_statement_timeout():
    q = "SELECT 1"
    cur = FakeCursor(
        fetch={q: [(1,)]}, description_for=lambda s: [Col("?column?")] if s == q else None
    )
    cols, rows, truncated = _db(cur).run_read_query(q, timeout_ms=500)
    assert cols == ["?column?"]
    assert rows == [(1,)] and truncated is False
    timeout = [(s, p) for s, p in cur.executed if s.startswith("SET LOCAL statement_timeout")]
    assert timeout and timeout[0][1] == (500,)
    assert cur.executed[0][0] == "BEGIN READ ONLY"
    assert cur.executed[-1][0] == "ROLLBACK"


def test_run_read_query_without_timeout_omits_set_local():
    q = "SELECT 1"
    cur = FakeCursor(
        fetch={q: [(1,)]}, description_for=lambda s: [Col("?column?")] if s == q else None
    )
    _db(cur).run_read_query(q, timeout_ms=None)
    assert not any(s.startswith("SET LOCAL statement_timeout") for s, _ in cur.executed)


# -- cell() coercion -----------------------------------------------------------


def test_cell_preserves_lists_and_dicts_recursively():
    assert cell(["a", "b"]) == ["a", "b"]
    assert cell([1, ["x", 2]]) == [1, ["x", 2]]
    assert cell({"k": [1, 2], "n": None}) == {"k": [1, 2], "n": None}


def test_cell_stringifies_unknown_scalars_and_passes_primitives():
    assert cell(date(2020, 1, 2)) == "2020-01-02"
    assert cell(None) is None
    assert cell(5) == 5 and cell(True) is True
