"""Offline tests for the decoupled DB capability model.

`DB_ADMIN` is superuser-only; `CREATEDB`/`CREATEROLE` are independent capabilities that
gate just `create_database` / `create_role`. No live DB — a fake db drives the probe.
"""

import pytest

from mcp_postgres.capabilities import (
    CapabilityError,
    CapabilityManager,
    DbTier,
    OsTier,
    enabled_tools_for,
)


class _FakeDb:
    def __init__(self, *, rolsuper=False, rolcreatedb=False, rolcreaterole=False, can_write=False):
        self.row = {
            "role": "mcp",
            "rolsuper": rolsuper,
            "rolcreatedb": rolcreatedb,
            "rolcreaterole": rolcreaterole,
        }
        self.can_write = can_write

    def query_one(self, sql, params=None):
        if "version()" in sql:  # environment.server_version probe
            return {
                "version_string": "PostgreSQL 16.0 (fake)",
                "server_version": "16.0",
                "version_num": 160000,
            }
        return dict(self.row)

    def query_scalar(self, sql):
        if "has_schema_privilege" in sql:
            return self.can_write
        if "config_file" in sql:
            return "/etc/postgresql/postgresql.conf"
        if "hba_file" in sql:
            return "/etc/postgresql/pg_hba.conf"
        return None

    def select(self, sql):  # environment.extensions probe
        return [], []


class _FakePriv:
    def check(self):
        return False


def _mgr(**kw):
    return CapabilityManager(_FakeDb(**kw), _FakePriv())


# -- tier + capability mapping -------------------------------------------------


def test_superuser_is_admin_with_both_caps():
    m = _mgr(rolsuper=True)
    assert m.db_tier() == DbTier.DB_ADMIN
    assert m.db_capabilities() == {"createdb": True, "createrole": True}


def test_createdb_only_is_not_admin():
    m = _mgr(rolcreatedb=True)  # not superuser, no schema CREATE
    assert m.db_tier() == DbTier.DB_READONLY
    caps = m.db_capabilities()
    assert caps["createdb"] is True
    assert caps["createrole"] is False


def test_createrole_only_is_not_admin():
    m = _mgr(rolcreaterole=True)
    assert m.db_tier() == DbTier.DB_READONLY
    assert m.db_capabilities() == {"createdb": False, "createrole": True}


def test_write_access_is_readwrite_not_admin():
    m = _mgr(can_write=True)
    assert m.db_tier() == DbTier.DB_READWRITE
    assert m.db_capabilities() == {"createdb": False, "createrole": False}


def test_plain_role_is_readonly():
    m = _mgr()
    assert m.db_tier() == DbTier.DB_READONLY


def test_createdb_role_that_can_also_write():
    m = _mgr(rolcreatedb=True, can_write=True)
    assert m.db_tier() == DbTier.DB_READWRITE
    assert m.db_capabilities()["createdb"] is True


# -- enabled_tools_for ---------------------------------------------------------


def test_enabled_tools_createdb_gets_create_database_only():
    tools = enabled_tools_for(OsTier.OS_NONE, DbTier.DB_READONLY, {"createdb": True})
    assert "create_database" in tools
    assert "create_role" not in tools
    for admin_only in ("grant", "revoke", "admin_sql"):
        assert admin_only not in tools


def test_enabled_tools_admin_gets_full_set():
    tools = enabled_tools_for(
        OsTier.OS_NONE, DbTier.DB_ADMIN, {"createdb": True, "createrole": True}
    )
    for t in ("create_database", "create_role", "grant", "revoke", "admin_sql"):
        assert t in tools


def test_enabled_tools_default_caps_empty():
    # 2-arg form (back-compat) offers no create_* tools, but admin-tier tools still gate.
    tools = enabled_tools_for(OsTier.OS_NONE, DbTier.DB_ADMIN)
    assert "create_database" not in tools
    assert "create_role" not in tools
    assert "grant" in tools


# -- guard(db_needs=...) -------------------------------------------------------


def test_guard_allows_when_capability_present():
    m = _mgr(rolcreatedb=True)
    assert m.guard(db_needs=("createdb",)) == []


def test_guard_refuses_when_capability_absent():
    m = _mgr()
    with pytest.raises(CapabilityError) as exc:
        m.guard(db_needs=("createdb",))
    msg = str(exc.value)
    assert "createdb" in msg
    assert "ALTER ROLE mcp CREATEDB" in msg


def test_guard_superuser_satisfies_createrole():
    m = _mgr(rolsuper=True)
    assert m.guard(db_needs=("createrole",)) == []


def test_report_exposes_capabilities_and_tools(monkeypatch):
    # report() pulls in environment.probe, which caches version/OS in module globals
    # process-wide. Reset them around this test so a fake never leaks into other tests.
    import mcp_postgres.environment as env

    monkeypatch.setattr(env, "_version_cache", None)
    monkeypatch.setattr(env, "_os_cache", None)

    m = _mgr(rolcreatedb=True)
    rep = m.report(database="postgres")
    assert rep["db_tier"] == "DB_READONLY"
    assert rep["db_capabilities"] == {"createdb": True, "createrole": False}
    assert "create_database" in rep["enabled_tools"]
    assert "grant" not in rep["enabled_tools"]
