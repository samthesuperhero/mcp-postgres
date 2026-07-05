"""Live observability & ops tests against the real cluster.

The read tools (`server_activity`, `list_locks`, `database_stats`, `get_settings`) are
always safe and run whenever the prod DB is reachable. The intervention tools
(`cancel_query`, `terminate_backend`) need `DB_ADMIN` and open a throwaway second
connection running `pg_sleep`, so they auto-skip when the role isn't a superuser.
Everything self-cleans: the sleeper is cancelled/terminated by the test itself.
"""

from __future__ import annotations

import threading
import time

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from mcp_postgres.capabilities import DbTier

pytestmark = pytest.mark.usefixtures("_live")

# Distinctive application_name so the test can find its own sleeper in pg_stat_activity.
MARKER = "mcp_obs_signaltest"


# -- read tools ----------------------------------------------------------------


def test_server_activity_includes_own_backend(tools):
    # The tool's own SELECT is an active client backend in the current DB, so it must
    # appear even with the idle-hiding, current-DB-only defaults.
    res = tools["server_activity"]()
    assert res["ok"] is True
    assert res["backend_count"] >= 1
    sample = res["backends"][0]
    assert {"pid", "state", "query", "query_runtime_s"} <= set(sample)


def test_server_activity_all_databases_and_idle(tools):
    res = tools["server_activity"](all_databases=True, include_idle=True, limit=5)
    assert res["ok"] is True
    assert len(res["backends"]) <= 5


def test_get_settings_exact_name(tools):
    res = tools["get_settings"](name="shared_buffers")
    assert res["ok"] is True
    assert res["setting_count"] == 1
    row = res["settings"][0]
    assert row["name"] == "shared_buffers"
    assert row["setting"]  # a non-empty value


def test_get_settings_prefix_matches_many(tools):
    res = tools["get_settings"](name="log_")
    assert res["ok"] is True
    assert res["setting_count"] > 1
    assert all(s["name"].startswith("log_") for s in res["settings"])


def test_list_locks_returns_a_list(tools):
    res = tools["list_locks"]()
    assert res["ok"] is True
    assert isinstance(res["blocked"], list)
    assert res["blocked_count"] == len(res["blocked"])


def test_database_stats_reports_current_db(tools, cfg):
    res = tools["database_stats"](top_tables=5)
    assert res["ok"] is True
    stats = res["stats"]
    assert stats["database"] == cfg.database.dbname
    assert isinstance(stats["size_bytes"], int) and stats["size_bytes"] > 0
    # Ratio is a float in [0, 1], or None on a brand-new DB with no block accesses.
    ratio = stats["cache_hit_ratio"]
    assert ratio is None or (0.0 <= float(ratio) <= 1.0)
    assert isinstance(res["top_tables"], list)


def test_read_tools_name_the_database(tools, cfg):
    for name, kwargs in [
        ("server_activity", {}),
        ("list_locks", {}),
        ("database_stats", {}),
        ("get_settings", {"name": "server_version"}),
    ]:
        res = tools[name](**kwargs)
        assert res.get("database") == cfg.database.dbname, name


# -- intervention tools (DB_ADMIN) ---------------------------------------------


def _admin_or_skip(caps):
    if caps.db_tier() < DbTier.DB_ADMIN:
        pytest.skip("cancel_query / terminate_backend need DB_ADMIN (superuser)")


def _conninfo(cfg):
    return make_conninfo(
        host=cfg.database.host,
        port=cfg.database.port,
        user=cfg.database.user,
        password=cfg.database.password,
        dbname=cfg.database.dbname,
        application_name=MARKER,
    )


def _run_sleeper(conninfo, errbox):
    """Open a fresh connection and run a long sleep; record whatever ends it."""
    try:
        with psycopg.connect(conninfo, connect_timeout=5) as conn:
            conn.execute("SELECT pg_sleep(30)")
    except Exception as exc:  # noqa: BLE001 - the cancel/terminate is what we assert on
        errbox.append(exc)


def _wait_for_marker_pid(tools, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = tools["server_activity"](all_databases=True, include_idle=True, limit=1000)
        for b in res["backends"]:
            if b.get("application_name") == MARKER:
                return b["pid"]
        time.sleep(0.2)
    return None


def _signal_sleeper(tools, cfg, tool_name):
    """Start a background sleeper, find its pid, signal it, and return (result, error)."""
    errbox: list = []
    th = threading.Thread(target=_run_sleeper, args=(_conninfo(cfg), errbox), daemon=True)
    th.start()
    try:
        pid = _wait_for_marker_pid(tools)
        assert pid is not None, "background sleeper never appeared in server_activity"
        res = tools[tool_name](pid=pid)
    finally:
        th.join(timeout=10)
    return res, (errbox[0] if errbox else None)


def test_cancel_query_stops_a_running_statement(tools, caps, cfg):
    _admin_or_skip(caps)
    res, err = _signal_sleeper(tools, cfg, "cancel_query")
    assert res["ok"] is True
    assert res["signalled"] is True
    # The sleeper's statement was cancelled (connection stays open, so this is the error).
    assert err is not None
    assert "cancel" in str(err).lower()


def test_terminate_backend_drops_the_connection(tools, caps, cfg):
    _admin_or_skip(caps)
    res, err = _signal_sleeper(tools, cfg, "terminate_backend")
    assert res["ok"] is True
    assert res["signalled"] is True
    assert err is not None
    assert "terminat" in str(err).lower() or "connection" in str(err).lower()
