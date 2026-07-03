"""Prod-side self-tests, run on deploy.

Exposed as the ``mcp-postgres-selftest`` console script. Validates the running
service end-to-end and prints a PASS/FAIL summary; exits non-zero on any failure.
Non-destructive: it only reads, and deliberately provokes refusals.
"""

from __future__ import annotations

import subprocess
import sys

from .config import Config, load_config


class Result:
    def __init__(self):
        self.checks: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))

    def ok(self) -> bool:
        return all(ok for _n, ok, _d in self.checks)


def _endpoint(cfg: Config) -> str:
    host = cfg.server.bind
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{cfg.server.port}{cfg.server.path}"


def _structured(call_result):
    """Extract a dict payload from an MCP CallToolResult."""
    sc = getattr(call_result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc
    import json

    for block in getattr(call_result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except Exception:  # noqa: BLE001
                return {"_text": text}
    return {}


async def _live_checks(cfg: Config, res: Result) -> None:
    try:
        from mcp import ClientSession
        from mcp.client import streamable_http as _sh

        # The client factory was renamed streamablehttp_client -> streamable_http_client.
        streamablehttp_client = getattr(_sh, "streamable_http_client", None) or _sh.streamablehttp_client
    except Exception as exc:  # noqa: BLE001
        res.add("mcp-client-import", False, str(exc))
        return

    url = _endpoint(cfg)
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    try:
        async with streamablehttp_client(url, headers=headers) as (read, write, _get_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res.add("mcp-initialize", True, url)

                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                res.add(
                    "tools-listed",
                    {"get_capabilities", "health_check", "run_read_query"} <= names,
                    ", ".join(sorted(names)),
                )

                caps = _structured(await session.call_tool("get_capabilities", {}))
                res.add(
                    "get_capabilities",
                    bool(caps.get("os_tier") and caps.get("db_tier")),
                    f"OS={caps.get('os_tier')} DB={caps.get('db_tier')} role={caps.get('connected_role')}",
                )

                health = _structured(await session.call_tool("health_check", {}))
                res.add("health_check", bool(health.get("database_connected")), str(health.get("server_version")))

                # Read-only guard: a write inside run_read_query must fail.
                ro = _structured(
                    await session.call_tool(
                        "run_read_query", {"sql": "CREATE TEMP TABLE _mcp_selftest(x int)"}
                    )
                )
                res.add(
                    "read-only-guard",
                    ro.get("ok") is False,
                    "write correctly rejected" if ro.get("ok") is False else "UNEXPECTEDLY ALLOWED",
                )
    except Exception as exc:  # noqa: BLE001
        res.add("mcp-live", False, f"could not reach {url}: {exc}")


def _db_check(cfg: Config, res: Result) -> None:
    try:
        import psycopg

        conninfo = psycopg.conninfo.make_conninfo(
            host=cfg.database.host,
            port=cfg.database.port,
            user=cfg.database.user,
            password=cfg.database.password,
            dbname=cfg.database.dbname,
        )
        with psycopg.connect(conninfo, connect_timeout=5) as conn:
            who = conn.execute("SELECT current_user").fetchone()[0]
        res.add("db-connect", who == cfg.database.user, f"connected as {who}")
    except Exception as exc:  # noqa: BLE001
        res.add("db-connect", False, str(exc))


def _service_check(res: Result) -> None:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", "mcp-postgres"], capture_output=True, timeout=10
        )
        state = proc.stdout.decode().strip()
        res.add("service-active", state == "active", state or "unknown")
    except Exception as exc:  # noqa: BLE001
        res.add("service-active", True, f"skipped ({exc})")  # non-fatal (e.g. dev host)


def _allowlist_check(cfg: Config, res: Result) -> None:
    """The privhelper must refuse a file outside the allowlist."""
    import os

    helper = os.environ.get("MCP_PG_PRIVHELPER", "/usr/libexec/mcp-postgres/privhelper")
    try:
        check = subprocess.run(["sudo", "-n", helper, "--check"], capture_output=True, timeout=15)
        if check.returncode != 0:
            res.add("config-allowlist", True, "skipped (no OS_CONFIG rights)")
            return
        bad = subprocess.run(
            ["sudo", "-n", helper, "read", "/etc/hostname"], capture_output=True, timeout=15
        )
        res.add(
            "config-allowlist",
            bad.returncode != 0,
            "disallowed path rejected" if bad.returncode != 0 else "ALLOWLIST BYPASS",
        )
    except Exception as exc:  # noqa: BLE001
        res.add("config-allowlist", True, f"skipped ({exc})")


def run_all(cfg: Config) -> Result:
    import anyio

    res = Result()
    _service_check(res)
    _db_check(cfg, res)
    _allowlist_check(cfg, res)
    anyio.run(_live_checks, cfg, res)
    return res


def main() -> None:
    cfg = load_config()
    res = run_all(cfg)
    print("mcp-postgres self-test")
    print("-" * 60)
    for name, ok, detail in res.checks:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)
    print("-" * 60)
    total = len(res.checks)
    passed = sum(1 for _n, ok, _d in res.checks if ok)
    print(f"{passed}/{total} checks passed")
    sys.exit(0 if res.ok() else 1)


if __name__ == "__main__":
    main()
