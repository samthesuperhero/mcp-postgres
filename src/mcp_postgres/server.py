"""MCP server entrypoint.

Builds a FastMCP server over the Streamable HTTP transport, wraps it in a pure
ASGI bearer-token middleware, and serves it with uvicorn.
"""

from __future__ import annotations

import logging

import uvicorn
from mcp.server.fastmcp import FastMCP

from .capabilities import CapabilityManager
from .config import Config, load_config
from .context import AppContext
from .db import Database
from .docs import REPO_URL, SERVER_INSTRUCTIONS
from .privclient import PrivClient
from .tools import admin, config_files, discovery, introspection, query

log = logging.getLogger("mcp_postgres")


class BearerAuthMiddleware:
    """Pure-ASGI middleware enforcing a static bearer token on the MCP path.

    Implemented as raw ASGI (not BaseHTTPMiddleware) so it does not buffer the
    long-lived Streamable HTTP responses.
    """

    def __init__(self, app, token: str, protected_path: str = "/mcp"):
        self.app = app
        self.token = token
        self.protected_path = protected_path

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and self.token and scope.get("path", "").startswith(
            self.protected_path
        ):
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth = headers.get(b"authorization", b"").decode(errors="replace")
            if auth != f"Bearer {self.token}":
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def build_server(cfg: Config) -> tuple[FastMCP, AppContext]:
    db = Database(cfg.database)
    db.open()
    priv = PrivClient()
    caps = CapabilityManager(db, priv)
    ctx = AppContext(config=cfg, db=db, caps=caps, priv=priv)

    mcp = FastMCP(
        "mcp-postgres",
        instructions=SERVER_INSTRUCTIONS,
        website_url=REPO_URL,
        host=cfg.server.bind,
        port=cfg.server.port,
        streamable_http_path=cfg.server.path,
    )

    introspection.register(mcp, ctx)
    query.register(mcp, ctx)
    admin.register(mcp, ctx)
    config_files.register(mcp, ctx)
    discovery.register(mcp, ctx)

    log.info(
        "capabilities at startup: OS=%s DB=%s; enabled tools: %s",
        caps.os_tier().name,
        caps.db_tier().name,
        ", ".join(sorted(ctx.enabled_tools)),
    )
    return mcp, ctx


def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not cfg.token:
        log.warning("no bearer token configured — the MCP endpoint is UNAUTHENTICATED")

    mcp, _ctx = build_server(cfg)
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware, token=cfg.token, protected_path=cfg.server.path)

    log.info("serving MCP on http://%s:%s%s", cfg.server.bind, cfg.server.port, cfg.server.path)
    uvicorn.run(app, host=cfg.server.bind, port=cfg.server.port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    main()
