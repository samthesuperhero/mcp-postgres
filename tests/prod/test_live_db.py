"""Live tests for the ``Database`` access layer against the real prod cluster.

The offline ``test_query_tools`` drives the same control flow with a fake cursor;
this suite proves it actually works against PostgreSQL — most importantly that the
timed read path (``run_read_query`` / ``explain`` with a ``statement_timeout``)
runs at all, and that the timeout is genuinely enforced, not merely accepted.

Regression guard (v0.9.0): the timeout was applied with ``SET LOCAL
statement_timeout = %s``. ``SET`` rejects bind parameters, so every read at the
default ``timeout_ms=30000`` failed on the real server while passing offline. These
tests exercise the real bind path so that can't recur.
"""

from __future__ import annotations

import psycopg
import pytest

pytestmark = pytest.mark.usefixtures("_live")


# -- run_read_query: the timed read path (the v0.9.0 regression) ---------------


def test_run_read_query_default_timeout_succeeds(live_db):
    # The exact failure mode: a plain read at the default 30s timeout must work.
    cols, rows, truncated = live_db.run_read_query("SELECT 1 AS n", timeout_ms=30000)
    assert cols == ["n"]
    assert rows == [(1,)]
    assert truncated is False


def test_run_read_query_small_timeout_still_returns(live_db):
    # A fast query well under a tight bound returns normally (the bound is applied,
    # not tripped).
    cols, rows, _ = live_db.run_read_query("SELECT 42 AS answer", timeout_ms=500)
    assert cols == ["answer"] and rows == [(42,)]


def test_run_read_query_no_timeout_succeeds(live_db):
    cols, rows, _ = live_db.run_read_query("SELECT 7 AS n", timeout_ms=None)
    assert rows == [(7,)]


def test_run_read_query_timeout_actually_cancels(live_db):
    # Proves the bound is enforced by PostgreSQL: a 2s sleep under a 200ms timeout is
    # cancelled. (If the SET were silently mis-issued the sleep would run to
    # completion and this would hang/return instead of raising.)
    with pytest.raises(psycopg.errors.QueryCanceled):
        live_db.run_read_query("SELECT pg_sleep(2)", timeout_ms=200)


def test_run_read_query_truncation_flag(live_db):
    cols, rows, truncated = live_db.run_read_query(
        "SELECT g FROM generate_series(1, 50) AS g", max_rows=10
    )
    assert cols == ["g"]
    assert len(rows) == 10
    assert truncated is True


def test_run_read_query_exact_fit_not_truncated(live_db):
    _cols, rows, truncated = live_db.run_read_query(
        "SELECT g FROM generate_series(1, 5) AS g", max_rows=5
    )
    assert len(rows) == 5
    assert truncated is False  # exactly max_rows, nothing beyond


def test_run_read_query_rejects_write(live_db):
    # The forced READ ONLY transaction blocks writes even for a role that could write.
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        live_db.run_read_query("CREATE TABLE _mcp_ro_probe (x int)")


def test_run_read_query_no_result_set(live_db):
    # A statement with no rows to return yields empty cols/rows, no crash.
    cols, rows, truncated = live_db.run_read_query("SELECT WHERE false")
    assert rows == [] and truncated is False


# -- explain -------------------------------------------------------------------


def test_explain_text_plan(live_db):
    fmt, plan = live_db.explain("SELECT 1")
    assert fmt == "text"
    assert isinstance(plan, str) and plan  # a non-empty textual plan


def test_explain_json_plan(live_db):
    fmt, plan = live_db.explain("SELECT 1", fmt="json")
    assert fmt == "json"
    # EXPLAIN (FORMAT JSON) returns a list holding one plan object with a "Plan" key.
    assert isinstance(plan, list) and plan and "Plan" in plan[0]


def test_explain_analyze_executes_and_is_safe(live_db):
    # ANALYZE really runs the query, but inside a rolled-back READ ONLY txn — so it
    # gathers real timings while discarding side effects.
    fmt, plan = live_db.explain(
        "SELECT count(*) FROM generate_series(1, 100)", analyze=True, fmt="json"
    )
    assert fmt == "json"
    assert "Actual Total Time" in plan[0]["Plan"]  # ANALYZE-only field


def test_explain_timeout_actually_cancels(live_db):
    with pytest.raises(psycopg.errors.QueryCanceled):
        live_db.explain("SELECT pg_sleep(2)", analyze=True, timeout_ms=200)


def test_explain_rejects_bad_format(live_db):
    with pytest.raises(ValueError):
        live_db.explain("SELECT 1", fmt="xml")


# -- select / query_one / query_scalar (trusted helpers) -----------------------


def test_select_returns_named_columns(live_db):
    cols, rows = live_db.select("SELECT 1 AS a, 'x' AS b")
    assert cols == ["a", "b"]
    assert rows == [(1, "x")]


def test_query_one_maps_columns_to_dict(live_db):
    row = live_db.query_one("SELECT current_database() AS db, current_user AS usr")
    assert set(row) == {"db", "usr"}
    assert row["usr"]  # the connected role name


def test_query_one_returns_none_on_empty(live_db):
    assert live_db.query_one("SELECT 1 WHERE false") is None


def test_query_scalar_returns_first_cell(live_db):
    assert live_db.query_scalar("SELECT 2 + 2") == 4
    assert live_db.query_scalar("SELECT 1 WHERE false") is None


def test_parameterised_query(live_db):
    # Ordinary (non-SET) statements take bind params, unlike the timeout path.
    cols, rows = live_db.select("SELECT %s::int AS v", (99,))
    assert rows == [(99,)]
