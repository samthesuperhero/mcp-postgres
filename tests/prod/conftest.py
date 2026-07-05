"""Shared fixtures for the live prod-DB test suite (``test_live_*.py``).

These tests exercise mcp-postgres against the **real** PostgreSQL the service is
configured to reach — ``load_config()`` reads ``/etc/mcp-postgres`` (overridable
with ``MCP_PG_CONFIG_DIR``), exactly as the running service does. On a host where
that database isn't reachable (e.g. a dev workstation with no cluster), every
fixture that needs it calls ``pytest.skip`` at collection, so the suite stays green
off-host while doing real, thorough work on the deployment host where the service
runs.

Footprint on the live database:

* The read paths (introspection, query, EXPLAIN, capability probe) are always safe
  and touch nothing.
* Write coverage (``writable_schema``) is gated on the connected role actually
  holding ``DB_READWRITE`` and is confined to a single throwaway schema
  (``mcp_prodtest``) that is dropped on the way in *and* out — nothing else in the
  database is ever modified.
* The service's own ``postgresql.conf`` / ``pg_hba.conf`` are never written by this
  suite; the config-file tools are covered offline in ``test_config_files_paths``.
"""

from __future__ import annotations

from types import SimpleNamespace

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from mcp_postgres.capabilities import DbTier
from mcp_postgres.config import load_config
from mcp_postgres.context import AppContext
from mcp_postgres.manager import DatabaseManager
from mcp_postgres.privclient import PrivClient
from mcp_postgres.tools import (
    admin,
    config_files,
    discovery,
    introspection,
    observability,
    prompts,
    query,
    schema,
)

# The isolated schema every write test lives in. Dropped before and after use, so a
# crashed run self-heals on the next one.
TEST_SCHEMA = "mcp_prodtest"


class CapturingMCP:
    """Stand-in for FastMCP that records the registered tool/resource closures.

    ``register(mcp, ctx)`` decorates nested functions with ``@mcp.tool`` /
    ``@mcp.resource``; capturing them lets a test invoke a tool directly with a real
    ``AppContext`` and assert on the plain-dict result — the full tool stack minus the
    HTTP/JSON-RPC transport (which ``test_live_service`` covers).
    """

    def __init__(self):
        self.tools: dict = {}
        self.resources: dict = {}
        self.prompts: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco

    def resource(self, *args, **kwargs):
        name = kwargs.get("name") or (args[0] if args else "resource")

        def deco(fn):
            self.resources[name] = fn
            return fn

        return deco

    def prompt(self, *args, **kwargs):
        name = kwargs.get("name")

        def deco(fn):
            self.prompts[name or fn.__name__] = fn
            return fn

        return deco


def _reachable(cfg) -> tuple[bool, str]:
    """Can we open a connection to the configured cluster? (bool, reason)."""
    conninfo = make_conninfo(
        host=cfg.database.host,
        port=cfg.database.port,
        user=cfg.database.user,
        password=cfg.database.password,
        dbname=cfg.database.dbname,
    )
    try:
        with psycopg.connect(conninfo, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
        return True, ""
    except Exception as exc:  # noqa: BLE001 - any failure means "skip on this host"
        return False, str(exc)


@pytest.fixture(scope="session")
def cfg():
    """The service's own configuration, loaded exactly as ``main()`` does."""
    return load_config()


@pytest.fixture(scope="session")
def _live(cfg):
    """Gate: skip the whole live suite unless the prod database is reachable."""
    ok, why = _reachable(cfg)
    if not ok:
        pytest.skip(
            f"prod PostgreSQL not reachable at {cfg.database.host}:{cfg.database.port} "
            f"as {cfg.database.user!r}: {why}"
        )
    return True


@pytest.fixture
def app(cfg, _live):
    """A real ``AppContext`` plus the registered tool/resource closures.

    One ``DatabaseManager`` (and thus one pool per touched database) per test, torn
    down afterwards so no test leaks connections into the next.
    """
    priv = PrivClient()
    manager = DatabaseManager(cfg.database, priv)
    ctx = AppContext(config=cfg, manager=manager, priv=priv)
    mcp = CapturingMCP()
    for mod in (
        introspection,
        schema,
        query,
        observability,
        admin,
        config_files,
        discovery,
        prompts,
    ):
        mod.register(mcp, ctx)
    try:
        yield SimpleNamespace(
            cfg=cfg,
            ctx=ctx,
            manager=manager,
            tools=mcp.tools,
            resources=mcp.resources,
            prompts=mcp.prompts,
        )
    finally:
        manager.close()


@pytest.fixture
def manager(app):
    return app.manager


@pytest.fixture
def target(app):
    return app.manager.current_target()


@pytest.fixture
def live_db(target):
    return target.db


@pytest.fixture
def caps(target):
    return target.caps


@pytest.fixture
def tools(app):
    return app.tools


@pytest.fixture
def writable_schema(app):
    """An isolated, always-cleaned schema for write tests; skips if the role is read-only.

    Requires the connected role to hold ``DB_READWRITE`` and to be able to create a
    schema in the current database. Yields the schema name; drops it (CASCADE) on
    teardown so nothing persists.
    """
    target = app.manager.current_target()
    if target.caps.db_tier() < DbTier.DB_READWRITE:
        pytest.skip(
            f"connected role is {target.caps.db_tier().name}; write tests need DB_READWRITE"
        )
    db = target.db
    try:
        db.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        db.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
    except Exception as exc:  # noqa: BLE001 - can write to public but not create a schema
        pytest.skip(f"cannot create an isolated test schema {TEST_SCHEMA!r}: {exc}")
    try:
        yield TEST_SCHEMA
    finally:
        try:
            db.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
