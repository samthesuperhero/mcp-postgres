"""Offline tests for the guided prompts and the schema collector.

The prompts are pure functions of their arguments, so they are checked by rendering them
directly. ``collect_db_schema`` is driven by a fake db that returns canned catalog rows,
verifying the assembly (grouping columns/PKs/FKs/enums per schema, the truncation cap)
without a live PostgreSQL — the live path is covered in ``test_live_guidance``.
"""

from mcp_postgres.tools import prompts
from mcp_postgres.tools.introspection import collect_db_schema


class _FakeMCP:
    """Captures the prompt functions register() decorates, keyed by name."""

    def __init__(self):
        self.prompts: dict = {}

    def prompt(self, *args, **kwargs):
        name = kwargs.get("name")

        def deco(fn):
            self.prompts[name or fn.__name__] = fn
            return fn

        return deco


def _prompts():
    m = _FakeMCP()
    prompts.register(m, ctx=None)  # recipes are static; ctx is unused
    return m.prompts


# -- prompts -------------------------------------------------------------------


def test_all_three_prompts_registered():
    assert set(_prompts()) == {"audit_privileges", "add_column_safely", "investigate_slow_query"}


def test_audit_privileges_recipe_names_tools_and_scopes_by_role():
    audit = _prompts()["audit_privileges"]
    text = audit()
    assert "get_capabilities" in text
    assert "run_read_query" in text
    assert "pg_roles" in text
    scoped = audit(role="alice")
    assert "alice" in scoped  # both the prose scope and the grantee filter


def test_add_column_safely_recipe_builds_the_ddl():
    text = _prompts()["add_column_safely"](
        table="app.users", column="age", column_type="int", default="0", not_null=True
    )
    assert "describe_table" in text
    assert "execute_batch" in text
    assert "ALTER TABLE app.users ADD COLUMN age int DEFAULT 0 NOT NULL" in text


def test_add_column_safely_recipe_omits_optional_clauses():
    text = _prompts()["add_column_safely"](table="t", column="c", column_type="text")
    assert "ALTER TABLE t ADD COLUMN c text" in text
    assert "DEFAULT" not in text.split("ALTER TABLE t ADD COLUMN c text")[1].split("\n")[0]
    assert "NOT NULL" not in text.split("ALTER TABLE t ADD COLUMN c text")[1].split("\n")[0]


def test_investigate_slow_query_recipe_embeds_query_and_obs_tools():
    text = _prompts()["investigate_slow_query"](query="SELECT * FROM big_table")
    assert "SELECT * FROM big_table" in text
    assert "explain_query" in text
    assert "server_activity" in text
    assert "list_locks" in text


# -- collect_db_schema ---------------------------------------------------------


class _SchemaFakeDb:
    """Returns canned catalog rows for the five schema-wide queries collect issues."""

    def query_scalar(self, sql, params=None):
        return "testdb"  # SELECT current_database()

    def select(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM pg_class c" in s and "relkind IN" in s:
            return [], [
                ("public", "users", "table"),
                ("public", "orders", "table"),
                ("public", "user_emails", "view"),
                ("shop", "widgets", "table"),
            ]
        if "information_schema.columns" in s:
            return [], [
                ("public", "users", "id", "integer", "NO", "nextval('users_id_seq')"),
                ("public", "users", "email", "text", "YES", None),
                ("public", "orders", "id", "integer", "NO", None),
                ("public", "orders", "user_id", "integer", "NO", None),
                ("shop", "widgets", "sku", "text", "NO", None),
            ]
        if "table_constraints" in s:
            return [], [
                ("public", "users", "id"),
                ("public", "orders", "id"),
            ]
        if "con.contype = 'f'" in s:
            return [], [
                ("public", "orders", ["user_id"], "public", "users", ["id"]),
            ]
        if "typtype = 'e'" in s:
            return [], [
                ("public", "order_status", ["new", "paid", "shipped"]),
            ]
        raise AssertionError(f"unexpected query: {s[:70]}")


def test_collect_db_schema_assembles_per_schema_structure():
    data = collect_db_schema(_SchemaFakeDb())
    assert data["database"] == "testdb"
    assert data["truncated"] is False
    assert data["table_count"] == 4

    schemas = {s["schema"]: s for s in data["schemas"]}
    assert set(schemas) == {"public", "shop"}

    users = next(t for t in schemas["public"]["tables"] if t["name"] == "users")
    assert users["kind"] == "table"
    assert [c["name"] for c in users["columns"]] == ["id", "email"]
    assert users["columns"][0]["nullable"] is False  # id NO
    assert users["columns"][1]["nullable"] is True  # email YES
    assert users["primary_key"] == ["id"]

    orders = next(t for t in schemas["public"]["tables"] if t["name"] == "orders")
    fk = orders["foreign_keys"][0]
    assert fk["columns"] == ["user_id"]
    assert fk["references"] == {"schema": "public", "table": "users", "columns": ["id"]}

    assert schemas["public"]["enums"] == [
        {"name": "order_status", "labels": ["new", "paid", "shipped"]}
    ]

    view = next(t for t in schemas["public"]["tables"] if t["name"] == "user_emails")
    assert view["kind"] == "view"
    assert view["primary_key"] == [] and view["foreign_keys"] == []


def test_collect_db_schema_truncates_at_cap():
    data = collect_db_schema(_SchemaFakeDb(), max_tables=2)
    assert data["truncated"] is True
    assert data["table_count"] == 2
    # Only the first two relations (both in public) survived the cap.
    assert {s["schema"] for s in data["schemas"]} == {"public"}
