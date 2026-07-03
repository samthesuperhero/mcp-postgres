"""Prod-side self-tests, run on deploy.

Exposed as the ``mcp-postgres-selftest`` console script. Validates the running
service end-to-end and prints a PASS/FAIL summary; exits non-zero on any failure.
Non-destructive: it only reads, and deliberately provokes refusals.
"""

from __future__ import annotations

import subprocess
import sys
from contextlib import asynccontextmanager

from .config import Config, load_config


class Result:
    def __init__(self):
        self.checks: list[tuple[str, bool, str]] = []
        self.extras: dict[str, str] = {}  # verbose-only extra detail, keyed by check name

    def add(self, name: str, ok: bool, detail: str = "", extra: str = "") -> None:
        self.checks.append((name, ok, detail))
        if extra:
            self.extras[name] = extra

    def ok(self) -> bool:
        return all(ok for _n, ok, _d in self.checks)


def _explain(exc: BaseException) -> str:
    """Recursively unwrap anyio/asyncio ExceptionGroups to their leaf cause(s)."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_explain(e) for e in exc.exceptions)
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


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


@asynccontextmanager
async def _streamable_client(url: str, headers: dict[str, str]):
    """Open a StreamableHTTP client transport across MCP SDK versions.

    Newer SDKs renamed the factory ``streamablehttp_client`` -> ``streamable_http_client``
    and dropped its ``headers`` kwarg — auth headers now live on a pre-built
    ``httpx.AsyncClient`` passed via ``http_client=``. Older SDKs still take
    ``streamablehttp_client(url, headers=...)``. Support both.
    """
    from mcp.client import streamable_http as _sh

    new_factory = getattr(_sh, "streamable_http_client", None)
    if new_factory is None:
        async with _sh.streamablehttp_client(url, headers=headers) as streams:
            yield streams
        return

    make_client = getattr(_sh, "create_mcp_http_client", None)
    if make_client is not None:
        client = make_client(headers=headers)  # applies MCP-recommended timeouts
    else:
        import httpx

        client = httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30.0, read=300.0))
    async with client:
        async with new_factory(url, http_client=client) as streams:
            yield streams


async def _live_checks(cfg: Config, res: Result) -> None:
    try:
        from mcp import ClientSession
    except Exception as exc:  # noqa: BLE001
        res.add("mcp-client-import", False, str(exc))
        return

    import json
    import traceback

    url = _endpoint(cfg)
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    try:
        async with _streamable_client(url, headers) as (read, write, _get_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res.add("mcp-initialize", True, url)

                tools = await session.list_tools()
                names = sorted(t.name for t in tools.tools)
                res.add(
                    "tools-listed",
                    {"get_capabilities", "health_check", "run_read_query"} <= set(names),
                    f"{len(names)} tools",
                    extra="tools: " + ", ".join(names),
                )

                caps = _structured(await session.call_tool("get_capabilities", {}))
                res.add(
                    "get_capabilities",
                    bool(caps.get("os_tier") and caps.get("db_tier")),
                    f"OS={caps.get('os_tier')} DB={caps.get('db_tier')} "
                    f"role={caps.get('connected_role')} v={caps.get('version')}",
                    extra=json.dumps(caps, indent=2, default=str),
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
        detail = _explain(exc)
        if "401" in detail:
            detail += (
                " — bearer token mismatch: the token the self-test sent does not match the "
                "running server's. Check /etc/mcp-postgres/token and restart the service so it "
                "reloads the current token."
            )
        res.add("mcp-live", False, f"could not reach {url}: {detail}", extra=traceback.format_exc())


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


def _print_preamble(cfg: Config) -> None:
    """Non-secret environment summary. Never prints the token or DB password."""
    from .docs import version

    tok = cfg.token or ""
    token_state = f"configured ({len(tok)} chars)" if tok else "NOT CONFIGURED"
    print(f"version:   {version()}")
    print(f"endpoint:  {_endpoint(cfg)}")
    print(f"token:     {token_state}")
    print(f"database:  {cfg.database.host}:{cfg.database.port} {cfg.database.user}/{cfg.database.dbname}")


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="mcp-postgres self-test")
    p.add_argument(
        "-q", "--quiet", action="store_true",
        help="terse PASS/FAIL summary only (default: full verbose output)",
    )
    verbose = not p.parse_args().quiet

    cfg = load_config()
    print("mcp-postgres self-test")
    print("-" * 60)
    if verbose:
        _print_preamble(cfg)
        print("-" * 60)

    res = run_all(cfg)
    for name, ok, detail in res.checks:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)
        if verbose and name in res.extras:
            for ln in res.extras[name].splitlines():
                print(f"           {ln}")
    print("-" * 60)
    total = len(res.checks)
    passed = sum(1 for _n, ok, _d in res.checks if ok)
    print(f"{passed}/{total} checks passed")
    sys.exit(0 if res.ok() else 1)


if __name__ == "__main__":
    main()
