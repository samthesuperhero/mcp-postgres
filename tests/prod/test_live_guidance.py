"""Live tests for the guided prompts and the schema resource, via the real register path.

The prompts render off any state, but the schema resource runs ``collect_db_schema``
against the real cluster, so these use the ``app`` fixture (which skips when the prod DB is
unreachable). They confirm the prompts are registered and render, and that
``schema://current`` reads back well-formed JSON describing the current database.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.usefixtures("_live")

PROMPTS = ("audit_privileges", "add_column_safely", "investigate_slow_query")


def test_prompts_registered_and_render(app):
    for name in PROMPTS:
        assert name in app.prompts, name
    assert app.prompts["audit_privileges"]().strip()
    assert app.prompts["investigate_slow_query"](query="SELECT 1").strip()
    ddl_recipe = app.prompts["add_column_safely"](table="t", column="c", column_type="int")
    assert "ALTER TABLE t ADD COLUMN c int" in ddl_recipe


def test_schema_resource_describes_current_database(app, cfg):
    payload = json.loads(app.resources["current database schema"]())
    assert payload["database"] == cfg.database.dbname
    assert isinstance(payload["schemas"], list)
    assert isinstance(payload["table_count"], int)
    assert payload["truncated"] in (True, False)
    # Every schema entry has the compact shape collect_db_schema promises.
    for s in payload["schemas"]:
        assert {"schema", "tables", "enums"} <= set(s)
        for tbl in s["tables"]:
            assert {"name", "kind", "columns", "primary_key", "foreign_keys"} <= set(tbl)
