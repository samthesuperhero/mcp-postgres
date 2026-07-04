"""Post-deploy: drive the read tools over the *running* MCP service.

Where ``test_live_tools`` exercises the current source in-process, this drives the
live service over Streamable HTTP — the exact surface the v0.9.0 read regression
surfaced on: ``run_read_query`` at the default ``timeout_ms`` returning
``ok=false`` because ``SET LOCAL statement_timeout = $1`` is rejected by PostgreSQL.

Like ``test_live_service``, it skips when the endpoint isn't reachable (a dev host),
so it's safe in a normal run and meaningful right after a deploy. A *reachable* but
still-broken service makes these fail rather than skip — which is the point.
"""

from __future__ import annotations

import pytest

from mcp_postgres.config import load_config
from mcp_postgres.selftest import (
    _endpoint,
    _is_conn_error,
    _streamable_client,
    _structured,
)


async def _drive_read_tools(cfg) -> dict:
    from mcp import ClientSession

    url = _endpoint(cfg)
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    async with _streamable_client(url, headers) as (read, write, _gid):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return {
                "read_default": _structured(
                    await session.call_tool("run_read_query", {"sql": "SELECT 1 AS n"})
                ),
                "read_zero": _structured(
                    await session.call_tool(
                        "run_read_query", {"sql": "SELECT 2 AS n", "timeout_ms": 0}
                    )
                ),
                "explain": _structured(
                    await session.call_tool("explain_query", {"sql": "SELECT 1"})
                ),
                "sample": _structured(
                    await session.call_tool(
                        "sample_table",
                        {"table": "pg_class", "schema": "pg_catalog", "limit": 3},
                    )
                ),
            }


@pytest.fixture(scope="module")
def service_reads():
    import anyio

    cfg = load_config()
    try:
        return anyio.run(_drive_read_tools, cfg)
    except Exception as exc:  # noqa: BLE001
        if _is_conn_error(exc):
            pytest.skip(f"MCP service not reachable at {_endpoint(cfg)}: {exc}")
        raise


def test_service_run_read_query_default_timeout(service_reads):
    # The primary read tool must work at its default settings over the live service.
    res = service_reads["read_default"]
    assert res.get("ok") is True, res
    assert res.get("rows") == [{"n": 1}]


def test_service_run_read_query_zero_timeout(service_reads):
    res = service_reads["read_zero"]
    assert res.get("ok") is True, res
    assert res.get("rows") == [{"n": 2}]


def test_service_explain_query(service_reads):
    res = service_reads["explain"]
    assert res.get("ok") is True, res
    assert res.get("plan")


def test_service_sample_table(service_reads):
    res = service_reads["sample"]
    assert res.get("ok") is True, res
    assert res.get("row_count") <= 3
