"""End-to-end tool tests against the real cluster.

Each tool module is registered with a real ``AppContext`` and the resulting closures
are invoked directly, so this covers the whole tool stack (guard → db layer →
result envelope) minus only the HTTP/JSON-RPC transport that ``test_live_service``
exercises. Targets are stable ``pg_catalog`` objects so the tests are deterministic
on any cluster, and every path is read-only.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.usefixtures("_live")

# Always-present catalog objects, so these tests don't depend on user schema.
CAT = "pg_catalog"
CAT_TABLE = "pg_class"  # relkind 'r', has indexes
CAT_VIEW = "pg_tables"  # a catalog view


# -- discovery / capability tools ----------------------------------------------


def test_get_capabilities_tool(tools, cfg):
    rep = tools["get_capabilities"]()
    assert rep["database"] == cfg.database.dbname
    assert rep["connected_role"] == cfg.database.user
    assert isinstance(rep["enabled_tools"], list) and rep["enabled_tools"]


def test_health_check_tool(tools, cfg):
    res = tools["health_check"]()
    assert res["ok"] is True
    assert res["database_connected"] is True
    assert res["server_version"].startswith("PostgreSQL")
    assert res["database"] == cfg.database.dbname
    assert res["db_tier"]  # populated when the DB is reachable


def test_use_database_tool_roundtrip(tools, cfg):
    res = tools["use_database"](name=cfg.database.dbname)
    assert res["ok"] is True
    assert res["database"] == cfg.database.dbname


def test_use_database_tool_bad_name(tools, cfg):
    res = tools["use_database"](name="mcp_no_such_db_zzz")
    assert res["ok"] is False
    assert "could not switch" in res["error"]
    # A failed switch leaves the current database intact.
    assert res["database"] == cfg.database.dbname


# -- introspection: listing ----------------------------------------------------


def test_list_databases_tool(tools, cfg):
    res = tools["list_databases"]()
    assert res["ok"] is True
    names = {d["datname"] for d in res["databases"]}
    assert cfg.database.dbname in names


def test_list_schemas_tool(tools):
    res = tools["list_schemas"]()
    assert res["ok"] is True
    # System schemas are filtered out.
    assert "pg_catalog" not in res["schemas"]
    assert "information_schema" not in res["schemas"]


def test_list_tables_tool(tools):
    res = tools["list_tables"](schema=CAT)
    assert res["ok"] is True
    names = {t["table_name"] for t in res["tables"]}
    assert CAT_TABLE in names


def test_describe_table_tool(tools):
    res = tools["describe_table"](table=CAT_TABLE, schema=CAT)
    assert res["ok"] is True
    assert res["kind"] == "table"
    colnames = {c["column_name"] for c in res["columns"]}
    assert {"relname", "relkind"} <= colnames
    assert res["indexes"]  # pg_class is indexed
    assert isinstance(res["foreign_keys"], list)
    assert isinstance(res["referenced_by"], list)
    assert res["total_bytes"] is not None


def test_describe_table_not_found(tools):
    res = tools["describe_table"](table="no_such_table_zzz", schema=CAT)
    assert res["ok"] is False
    assert "not found" in res["error"]


def test_list_indexes_tool(tools):
    res = tools["list_indexes"](schema=CAT, table=CAT_TABLE)
    assert res["ok"] is True
    assert res["indexes"]
    assert all(ix["table"] == CAT_TABLE for ix in res["indexes"])


def test_list_views_tool(tools):
    res = tools["list_views"](schema=CAT)
    assert res["ok"] is True
    names = {v["name"] for v in res["views"]}
    assert CAT_VIEW in names


def test_list_views_with_definition(tools):
    res = tools["list_views"](schema=CAT, include_definition=True)
    assert res["ok"] is True
    view = next(v for v in res["views"] if v["name"] == CAT_VIEW)
    assert "select" in view["definition"].lower()


def test_list_functions_tool(tools):
    res = tools["list_functions"](schema=CAT)
    assert res["ok"] is True
    assert res["functions"]  # pg_catalog defines many functions
    sample = res["functions"][0]
    assert set(sample) >= {"name", "arguments", "returns", "language", "kind"}


def test_list_foreign_keys_tool(tools):
    # pg_catalog has no FKs, but the tool must still succeed with an empty list.
    res = tools["list_foreign_keys"](schema=CAT)
    assert res["ok"] is True
    assert isinstance(res["foreign_keys"], list)


def test_list_enums_tool(tools):
    res = tools["list_enums"](schema=CAT)
    assert res["ok"] is True
    assert isinstance(res["enums"], list)


def test_get_object_definition_view(tools):
    res = tools["get_object_definition"](kind="view", name=CAT_VIEW, schema=CAT)
    assert res["ok"] is True
    assert "select" in res["definition"].lower()


def test_get_object_definition_unsupported_kind(tools):
    res = tools["get_object_definition"](kind="trigger", name="x", schema=CAT)
    assert res["ok"] is False
    assert "unsupported kind" in res["error"]


def test_get_object_definition_not_found(tools):
    res = tools["get_object_definition"](kind="view", name="no_such_view_zzz", schema=CAT)
    assert res["ok"] is False
    assert "not found" in res["error"]


# -- query tools: the read paths (the v0.9.0 regression, end-to-end) -----------


def test_run_read_query_tool_default_timeout(tools):
    # The user-facing regression: default timeout_ms must not break the primary tool.
    res = tools["run_read_query"](sql="SELECT 1 AS n")
    assert res["ok"] is True, res.get("error")
    assert res["rows"] == [{"n": 1}]
    assert res["truncated"] is False


def test_run_read_query_tool_explicit_timeout(tools):
    res = tools["run_read_query"](sql="SELECT 'hi' AS greeting", timeout_ms=1000)
    assert res["ok"] is True, res.get("error")
    assert res["rows"] == [{"greeting": "hi"}]


def test_run_read_query_tool_zero_timeout(tools):
    # timeout_ms=0 disables the bound; documented as the workaround for the bug and
    # must keep working after the fix.
    res = tools["run_read_query"](sql="SELECT 5 AS n", timeout_ms=0)
    assert res["ok"] is True
    assert res["rows"] == [{"n": 5}]


def test_run_read_query_tool_max_rows(tools):
    res = tools["run_read_query"](
        sql="SELECT g FROM generate_series(1, 100) g", max_rows=10
    )
    assert res["ok"] is True
    assert res["row_count"] == 10
    assert res["truncated"] is True


def test_run_read_query_tool_rejects_write(tools):
    res = tools["run_read_query"](sql="CREATE TABLE _mcp_probe (x int)")
    assert res["ok"] is False
    assert "read-only" in res["error"].lower()


def test_run_read_query_tool_reports_sql_error(tools):
    res = tools["run_read_query"](sql="SELECT * FROM definitely_no_such_table_zzz")
    assert res["ok"] is False
    assert res["error"]


def test_explain_query_tool_text(tools):
    res = tools["explain_query"](sql="SELECT 1")
    assert res["ok"] is True
    assert res["format"] == "text"
    assert isinstance(res["plan"], str) and res["plan"]


def test_explain_query_tool_json_analyze(tools):
    res = tools["explain_query"](
        sql="SELECT count(*) FROM generate_series(1, 10)", analyze=True, format="json"
    )
    assert res["ok"] is True
    assert res["analyze"] is True
    assert res["format"] == "json"
    assert "Plan" in res["plan"][0]


def test_sample_table_tool(tools):
    res = tools["sample_table"](table=CAT_TABLE, schema=CAT, limit=3)
    assert res["ok"] is True
    assert res["row_count"] <= 3
    assert res["schema"] == CAT and res["table"] == CAT_TABLE


def test_sample_table_tool_not_found(tools):
    res = tools["sample_table"](table="no_such_table_zzz", schema=CAT)
    assert res["ok"] is False
    assert res["error"]


# -- envelope invariants -------------------------------------------------------


def test_every_read_result_names_the_database(tools, cfg):
    calls = [
        ("list_databases", {}),
        ("list_schemas", {}),
        ("list_tables", {"schema": CAT}),
        ("describe_table", {"table": CAT_TABLE, "schema": CAT}),
        ("run_read_query", {"sql": "SELECT 1"}),
        ("explain_query", {"sql": "SELECT 1"}),
        ("sample_table", {"table": CAT_TABLE, "schema": CAT}),
        ("list_functions", {"schema": CAT}),
    ]
    for name, kwargs in calls:
        res = tools[name](**kwargs)
        assert res.get("database") == cfg.database.dbname, name


# -- discovery resource --------------------------------------------------------


def test_capabilities_resource_matches_report(app, cfg):
    payload = json.loads(app.resources["current capabilities"]())
    assert payload["database"] == cfg.database.dbname
    assert payload["connected_role"] == cfg.database.user
    assert "enabled_tools" in payload
