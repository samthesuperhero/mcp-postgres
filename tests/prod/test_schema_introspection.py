"""Offline tests for schema-introspection helpers and tool gating.

The introspection SQL runs against a live cluster in the self-test; here we cover the
pure Python around it — foreign-key/action parsing, object-definition resolution
(overload disambiguation, wrong-kind / not-found errors), and that the new read tools
are advertised at ``DB_READONLY`` while ``execute_batch`` needs ``DB_READWRITE``.
"""

import pytest

from mcp_postgres.capabilities import DbTier, OsTier, enabled_tools_for
from mcp_postgres.tools.introspection import _FK_ACTIONS, _foreign_keys, _referenced_by
from mcp_postgres.tools.schema import _function_def, _relation_def


class FakeDb:
    """Minimal db stub: canned results for select/query_one/query_scalar."""

    def __init__(self, select_result=([], []), one=None, scalar=None):
        self._select = select_result
        self._one = one
        self._scalar = scalar
        self.calls = []

    def select(self, sql, params=None):
        self.calls.append((sql, params))
        return self._select

    def query_one(self, sql, params=None):
        return self._one

    def query_scalar(self, sql, params=None):
        return self._scalar


# -- foreign-key parsing -------------------------------------------------------


def test_fk_action_codes_cover_postgres_set():
    assert _FK_ACTIONS == {
        "a": "NO ACTION",
        "r": "RESTRICT",
        "c": "CASCADE",
        "n": "SET NULL",
        "d": "SET DEFAULT",
    }


def test_foreign_keys_maps_actions_and_column_lists():
    rows = [(
        "fk_x", ["a", "b"], "public", "parent", ["id", "k"],
        "c", "n", "FOREIGN KEY (a, b) REFERENCES parent(id, k)",
    )]
    cols = ["name", "columns", "foreign_schema", "foreign_table",
            "foreign_columns", "on_update", "on_delete", "definition"]
    fks = _foreign_keys(FakeDb(select_result=(cols, rows)), 42)
    assert fks[0]["columns"] == ["a", "b"]
    assert fks[0]["foreign_columns"] == ["id", "k"]
    assert fks[0]["foreign_table"] == "parent"
    assert fks[0]["on_update"] == "CASCADE"
    assert fks[0]["on_delete"] == "SET NULL"


def test_referenced_by_lists_referencing_tables():
    rows = [("public", "child", "child_parent_fkey", "FOREIGN KEY (pid) REFERENCES parent(id)")]
    refs = _referenced_by(FakeDb(select_result=(["schema", "table", "name", "definition"], rows)), 1)
    assert refs == [{
        "schema": "public",
        "table": "child",
        "name": "child_parent_fkey",
        "definition": "FOREIGN KEY (pid) REFERENCES parent(id)",
    }]


# -- get_object_definition resolution ------------------------------------------


def test_function_def_overloaded_lists_candidates():
    db = FakeDb(select_result=(["oid", "args"], [(1, "integer"), (2, "text")]))
    with pytest.raises(LookupError) as exc:
        _function_def(db, "public", "f")
    msg = str(exc.value)
    assert "overloaded" in msg and "f(integer)" in msg and "f(text)" in msg


def test_function_def_not_found():
    with pytest.raises(LookupError):
        _function_def(FakeDb(select_result=(["oid", "args"], [])), "public", "nope")


def test_function_def_single_match_returns_ddl():
    db = FakeDb(select_result=(["oid", "args"], [(7, "integer")]), scalar="CREATE FUNCTION f(...)")
    assert _function_def(db, "public", "f") == "CREATE FUNCTION f(...)"


def test_relation_def_wrong_kind():
    db = FakeDb(one={"oid": 5, "relkind": "r"})  # a table, asked for a view
    with pytest.raises(LookupError) as exc:
        _relation_def(db, "public", "t", "view")
    assert "not a view" in str(exc.value)


def test_relation_def_not_found():
    with pytest.raises(LookupError):
        _relation_def(FakeDb(one=None), "public", "v", "view")


def test_relation_def_index_uses_indexdef():
    db = FakeDb(one={"oid": 9, "relkind": "i"}, scalar="CREATE INDEX idx ON t (a)")
    assert _relation_def(db, "public", "idx", "index") == "CREATE INDEX idx ON t (a)"


# -- tool gating ---------------------------------------------------------------


def test_new_read_tools_always_enabled_at_readonly():
    tools = enabled_tools_for(OsTier.OS_NONE, DbTier.DB_READONLY)
    for name in (
        "list_foreign_keys", "list_indexes", "list_views", "list_functions",
        "list_enums", "get_object_definition", "explain_query", "sample_table",
    ):
        assert name in tools, name
    assert "execute_batch" not in tools  # needs write


def test_execute_batch_gated_on_readwrite():
    assert "execute_batch" in enabled_tools_for(OsTier.OS_NONE, DbTier.DB_READWRITE)
