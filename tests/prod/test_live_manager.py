"""Live tests for the per-database target registry (``DatabaseManager``).

Switching databases, lazy pool creation/caching, and the invariant that a failed
switch never strands the session — all against the real cluster.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("_live")

# A name no real database will have; used to drive the failure path of use().
BOGUS_DB = "mcp_no_such_database_zzz"


def test_initial_current_is_default(manager, cfg):
    assert manager.current == cfg.database.dbname
    assert manager.default == cfg.database.dbname


def test_current_target_connects_and_probes(manager, cfg):
    target = manager.current_target()
    assert target.dbname == cfg.database.dbname
    # A live probe with no error means the pool really connected.
    assert target.caps.db_info(force=True)["error"] is None


def test_get_caches_target(manager, cfg):
    a = manager.get(cfg.database.dbname)
    b = manager.get(cfg.database.dbname)
    assert a is b  # same Target (and thus same pool) reused


def test_use_default_database_ok(manager, cfg):
    target = manager.use(cfg.database.dbname)
    assert target.dbname == cfg.database.dbname
    assert manager.current == cfg.database.dbname


def test_use_unknown_database_raises_and_preserves_current(manager, cfg):
    before = manager.current
    with pytest.raises(ConnectionError):
        manager.use(BOGUS_DB)
    # Current target unchanged and the failed name is not left cached.
    assert manager.current == before
    assert BOGUS_DB not in manager._targets


def test_switch_across_connectable_databases(manager, live_db, cfg):
    # Every database role `mcp` may CONNECT to should be switchable, and the target
    # must report itself back. (Restricted to a handful to keep the test quick.)
    _cols, rows = live_db.select(
        "SELECT datname FROM pg_database "
        "WHERE datistemplate = false AND datallowconn "
        "  AND has_database_privilege(current_user, datname, 'CONNECT') "
        "ORDER BY datname"
    )
    names = [r[0] for r in rows]
    assert cfg.database.dbname in names  # the default must be connectable

    for name in names[:5]:
        target = manager.use(name)
        assert target.dbname == name
        assert manager.current == name
        assert target.caps.report(database=name)["database"] == name
