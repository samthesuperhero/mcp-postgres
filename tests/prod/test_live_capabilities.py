"""Live capability probe against the real cluster.

The offline ``test_capabilities`` drives the tier/capability logic with a fake db;
here the probe runs against the actual role ``mcp`` connects as, so the reported
tier, attributes, and enabled-tool set reflect the deployment — and are
cross-checked against what PostgreSQL itself says about the role.
"""

from __future__ import annotations

import pytest

from mcp_postgres.capabilities import (
    CapabilityError,
    DbTier,
    enabled_tools_for,
)

pytestmark = pytest.mark.usefixtures("_live")


def test_probe_identifies_connected_role(caps, cfg):
    info = caps.db_info(force=True)
    assert info["error"] is None
    assert info["role"] == cfg.database.user


def test_tier_matches_role_attributes(caps, live_db):
    info = caps.db_info(force=True)
    attrs = info["attributes"]
    # Confirm the probe's attributes against the catalog directly.
    row = live_db.query_one(
        "SELECT rolsuper, rolcreatedb, rolcreaterole "
        "FROM pg_roles WHERE rolname = current_user"
    )
    assert attrs["superuser"] == bool(row["rolsuper"])
    assert attrs["createdb"] == bool(row["rolcreatedb"])
    assert attrs["createrole"] == bool(row["rolcreaterole"])

    # The tier is derived from superuser / write access with a strict ordering.
    can_write = bool(
        live_db.query_scalar("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
    )
    if attrs["superuser"]:
        assert info["tier"] == DbTier.DB_ADMIN
    elif can_write:
        assert info["tier"] == DbTier.DB_READWRITE
    else:
        assert info["tier"] == DbTier.DB_READONLY


def test_capabilities_fold_superuser(caps):
    info = caps.db_info(force=True)
    attrs, capabilities = info["attributes"], info["capabilities"]
    # Superuser implies both create capabilities; otherwise they mirror the attribute.
    assert capabilities["createdb"] == (attrs["superuser"] or attrs["createdb"])
    assert capabilities["createrole"] == (attrs["superuser"] or attrs["createrole"])


def test_config_and_hba_paths_discovered(caps):
    # Visible to any role; used to locate the editable files. A live server always
    # reports absolute paths for these.
    info = caps.db_info(force=True)
    assert info["config_file"] and info["config_file"].endswith(".conf")
    assert info["hba_file"] and info["hba_file"].endswith(".conf")


def test_report_is_well_formed(caps, cfg):
    rep = caps.report(database=cfg.database.dbname)
    for key in (
        "service",
        "version",
        "database",
        "os_tier",
        "db_tier",
        "connected_role",
        "role_attributes",
        "db_capabilities",
        "environment",
        "enabled_tools",
        "checked_at",
    ):
        assert key in rep, key
    assert rep["service"] == "mcp-postgres"
    assert rep["database"] == cfg.database.dbname
    assert rep["connected_role"] == cfg.database.user
    assert rep["database_error"] is None


def test_enabled_tools_consistent_with_tiers(caps):
    rep = caps.report()
    expected = enabled_tools_for(
        caps.os_tier(force=True), caps.db_tier(force=True), caps.db_capabilities(force=True)
    )
    assert rep["enabled_tools"] == expected
    # The always-on read tools are present at every tier.
    for name in (
        "get_capabilities",
        "health_check",
        "list_tables",
        "run_read_query",
        "explain_query",
        "sample_table",
    ):
        assert name in rep["enabled_tools"], name


def test_write_tools_gated_on_tier(caps):
    tools = set(caps.report()["enabled_tools"])
    tier = caps.db_tier(force=True)
    if tier >= DbTier.DB_READWRITE:
        assert {"execute_sql", "execute_batch"} <= tools
    else:
        assert not ({"execute_sql", "execute_batch"} & tools)
    if tier >= DbTier.DB_ADMIN:
        assert {"grant", "revoke", "admin_sql"} <= tools
    else:
        assert not ({"grant", "revoke", "admin_sql"} & tools)


# -- guard behaviour at the real tier ------------------------------------------


def test_guard_readonly_always_allowed(caps):
    # Returns a (possibly empty) notices list; never raises at the base tier.
    assert isinstance(caps.guard(db_min=DbTier.DB_READONLY), list)


def test_guard_admin_enforced_against_real_tier(caps):
    tier = caps.db_tier(force=True)
    if tier >= DbTier.DB_ADMIN:
        assert isinstance(caps.guard(db_min=DbTier.DB_ADMIN), list)
    else:
        with pytest.raises(CapabilityError) as exc:
            caps.guard(db_min=DbTier.DB_ADMIN)
        assert "DB_ADMIN" in str(exc.value)


def test_guard_createdb_matches_capability(caps):
    has = caps.db_capabilities(force=True)["createdb"]
    if has:
        assert caps.guard(db_needs=("createdb",)) == []
    else:
        with pytest.raises(CapabilityError):
            caps.guard(db_needs=("createdb",))
