"""Live write-path tests: ``execute_sql`` and ``execute_batch`` against the real DB.

Every test here runs only when the connected role actually holds ``DB_READWRITE``
(otherwise the ``writable_schema`` fixture skips) and confines all writes to a
single throwaway schema that is dropped on the way in and out. Nothing outside that
schema is ever touched.

These prove the transaction machinery end-to-end on PostgreSQL: atomic all-or-nothing
batches, best-effort partial commits, and that committed data is actually visible on
a subsequent read.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("_live")


def _scalar(tools, sql):
    res = tools["run_read_query"](sql=sql)
    assert res["ok"] is True, res.get("error")
    return next(iter(res["rows"][0].values())) if res["rows"] else None


# -- execute_sql ---------------------------------------------------------------


def test_execute_sql_ddl_dml_and_readback(tools, writable_schema):
    s = writable_schema
    created = tools["execute_sql"](sql=f"CREATE TABLE {s}.t (id int, label text)")
    assert created["ok"] is True
    assert "CREATE TABLE" in created["status"]

    inserted = tools["execute_sql"](
        sql=f"INSERT INTO {s}.t VALUES (1, 'a'), (2, 'b')"
    )
    assert inserted["ok"] is True
    assert inserted["rowcount"] == 2

    # The committed rows are visible on a fresh read.
    assert _scalar(tools, f"SELECT count(*) FROM {s}.t") == 2


def test_execute_sql_returns_result_rows(tools, writable_schema):
    s = writable_schema
    tools["execute_sql"](sql=f"CREATE TABLE {s}.t (id int)")
    tools["execute_sql"](sql=f"INSERT INTO {s}.t VALUES (10), (20)")
    res = tools["execute_sql"](sql=f"SELECT id FROM {s}.t ORDER BY id")
    assert res["ok"] is True
    assert res["columns"] == ["id"]
    assert res["rows"] == [[10], [20]]


def test_execute_sql_reports_error(tools, writable_schema):
    res = tools["execute_sql"](sql="THIS IS NOT VALID SQL")
    assert res["ok"] is False
    assert res["error"]


# -- execute_batch -------------------------------------------------------------


def test_execute_batch_atomic_rollback_leaves_nothing(tools, writable_schema):
    s = writable_schema
    res = tools["execute_batch"](
        statements=[
            f"CREATE TABLE {s}.t_atomic (id int)",
            f"INSERT INTO {s}.t_atomic VALUES (1)",
            "NOT SQL AT ALL",
        ]
    )
    assert res["committed"] is False
    assert res["ok"] is False
    assert res["failed_index"] == 2
    assert [r["ok"] for r in res["results"]] == [True, True, False]
    # Atomic guarantee: the table the batch started to build must not exist.
    assert _scalar(tools, f"SELECT to_regclass('{s}.t_atomic')") is None


def test_execute_batch_best_effort_commits_successes(tools, writable_schema):
    s = writable_schema
    res = tools["execute_batch"](
        statements=[
            f"CREATE TABLE {s}.t_be (id int)",
            "NOT SQL AT ALL",
            f"INSERT INTO {s}.t_be VALUES (1)",
        ],
        stop_on_error=False,
    )
    assert res["committed"] is True
    assert res["ok"] is True
    assert res["failed_index"] == 1  # first failure still reported
    assert [r["ok"] for r in res["results"]] == [True, False, True]
    # The surviving statements committed together.
    assert _scalar(tools, f"SELECT count(*) FROM {s}.t_be") == 1


def test_execute_batch_all_ok_commits(tools, writable_schema):
    s = writable_schema
    res = tools["execute_batch"](
        statements=[
            f"CREATE TABLE {s}.t_ok (id int)",
            f"INSERT INTO {s}.t_ok VALUES (1), (2), (3)",
        ]
    )
    assert res["committed"] is True
    assert res["ok"] is True
    assert "failed_index" not in res
    assert _scalar(tools, f"SELECT count(*) FROM {s}.t_ok") == 3


def test_execute_batch_empty_is_rejected(tools, writable_schema):
    res = tools["execute_batch"](statements=[])
    assert res["ok"] is False
    assert "no statements" in res["error"]


def test_execute_batch_result_carries_database(tools, writable_schema, cfg):
    res = tools["execute_batch"](statements=[f"SELECT 1"])
    assert res["database"] == cfg.database.dbname
