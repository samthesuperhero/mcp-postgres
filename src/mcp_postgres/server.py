"""MCP server entrypoint.

Builds a FastMCP server over the Streamable HTTP transport, wraps it in a pure
ASGI bearer-token middleware, and serves it with uvicorn.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .capabilities import enabled_tools_for
from .config import Config, check_config, load_config
from .context import AppContext
from .docs import REPO_URL, SERVER_INSTRUCTIONS
from .manager import DatabaseManager
from .oauth import OAuthStore, PostgresOAuthProvider
from .oauth.login import register_login_routes
from .privclient import PrivClient
from .tools import admin, config_files, discovery, introspection, query, schema

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


def resolve_oauth(cfg: Config) -> tuple[PostgresOAuthProvider | None, AuthSettings | None]:
    """Build the OAuth provider + settings, or ``(None, None)`` for static-only auth.

    Returns ``(None, None)`` when OAuth is disabled, or when it is enabled but
    misconfigured (missing/invalid ``public_url``) — in which case we log an error
    and fall back to static-bearer auth rather than refusing to start.
    """
    if not cfg.oauth.enabled:
        return None, None
    if not cfg.oauth.public_url:
        log.error(
            "oauth.enabled is true but oauth.public_url is unset — "
            "falling back to static bearer auth (set the public HTTPS base URL to enable OAuth)"
        )
        return None, None

    issuer = cfg.oauth.public_url.rstrip("/")
    try:
        # Validate the issuer the same way the SDK will when it mounts the routes,
        # so a bad URL fails here (graceful fallback) instead of crashing startup.
        from mcp.server.auth.routes import validate_issuer_url
        from pydantic import AnyHttpUrl

        validate_issuer_url(AnyHttpUrl(issuer))
        auth_settings = AuthSettings(
            issuer_url=issuer,
            resource_server_url=issuer + cfg.server.path,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "oauth.public_url %r is not a valid issuer (%s) — falling back to static bearer auth",
            cfg.oauth.public_url,
            exc,
        )
        return None, None

    store = OAuthStore(Path(cfg.oauth.state_dir) / "oauth.db")
    provider = PostgresOAuthProvider(
        store,
        static_token=cfg.token,
        access_token_ttl=cfg.oauth.access_token_ttl,
        refresh_token_ttl=cfg.oauth.refresh_token_ttl,
    )
    log.info(
        "OAuth 2.1 layer enabled: issuer=%s resource=%s (static bearer token still accepted)",
        auth_settings.issuer_url,
        auth_settings.resource_server_url,
    )
    return provider, auth_settings


def _transport_security(cfg: Config) -> TransportSecuritySettings:
    """Allowlist the Host/Origin values the reverse proxy forwards.

    Binding ``127.0.0.1`` makes FastMCP auto-enable DNS-rebinding protection that
    accepts *only* localhost Host headers — which rejects (HTTP 421) every request
    nginx forwards with the real public Host (e.g. ``db.example.com``). We keep the
    protection on but widen the allowlist to the public host from
    ``oauth.public_url`` (plus localhost and the bind address), so remote clients
    like the claude.ai web connector are accepted while nginx keeps forwarding the
    genuine Host. Only used when OAuth is on (that's when a public host is known).
    """
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if cfg.server.bind and cfg.server.bind not in ("127.0.0.1", "localhost", "::1"):
        hosts.append(f"{cfg.server.bind}:*")
    parsed = urlparse(cfg.oauth.public_url)
    if parsed.hostname:
        # Exact (Host without a port, e.g. behind :443) plus any :port.
        hosts.extend([parsed.hostname, f"{parsed.hostname}:*"])
        origin = f"{parsed.scheme}://{parsed.hostname}"
        origins.extend([origin, f"{origin}:*"])
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def build_server(
    cfg: Config,
    oauth_provider: PostgresOAuthProvider | None = None,
    auth_settings: AuthSettings | None = None,
) -> tuple[FastMCP, AppContext]:
    priv = PrivClient()
    manager = DatabaseManager(cfg.database, priv)
    ctx = AppContext(config=cfg, manager=manager, priv=priv)

    auth_kwargs = {}
    if oauth_provider is not None and auth_settings is not None:
        # OAuth means remote clients reach us through nginx with the public Host —
        # widen the DNS-rebinding allowlist so those requests aren't 421'd.
        auth_kwargs = {
            "auth_server_provider": oauth_provider,
            "auth": auth_settings,
            "transport_security": _transport_security(cfg),
        }

    mcp = FastMCP(
        "mcp-postgres",
        instructions=SERVER_INSTRUCTIONS,
        website_url=REPO_URL,
        host=cfg.server.bind,
        port=cfg.server.port,
        streamable_http_path=cfg.server.path,
        **auth_kwargs,
    )

    introspection.register(mcp, ctx)
    schema.register(mcp, ctx)
    query.register(mcp, ctx)
    admin.register(mcp, ctx)
    config_files.register(mcp, ctx)
    discovery.register(mcp, ctx)

    # The login page that gates the OAuth /authorize step (unauthenticated route).
    if oauth_provider is not None:
        register_login_routes(mcp, oauth_provider, cfg.token)

    # Probe the default (initial current) database for the startup capability log.
    current = manager.current_target()
    os_t, db_t = current.caps.os_tier(), current.caps.db_tier()
    db_caps = current.caps.db_capabilities()
    log.info(
        "capabilities at startup: db=%s OS=%s DB=%s; enabled tools: %s",
        current.dbname,
        os_t.name,
        db_t.name,
        ", ".join(enabled_tools_for(os_t, db_t, db_caps)),
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

    # Advisory only: report config.toml drift from the current schema (new keys
    # running on defaults, deprecated/unrecognized keys). Never fatal.
    for line in check_config(cfg.config_dir).messages():
        log.warning("config: %s", line)

    oauth_provider, auth_settings = resolve_oauth(cfg)
    mcp, _ctx = build_server(cfg, oauth_provider, auth_settings)
    app = mcp.streamable_http_app()
    if oauth_provider is None:
        # Static-bearer mode: guard /mcp ourselves. When OAuth is active the SDK's
        # RequireAuthMiddleware guards it instead (and honors the static token via
        # the provider's load_access_token), so this middleware must not be added.
        app.add_middleware(BearerAuthMiddleware, token=cfg.token, protected_path=cfg.server.path)

    log.info("serving MCP on http://%s:%s%s", cfg.server.bind, cfg.server.port, cfg.server.path)
    uvicorn.run(app, host=cfg.server.bind, port=cfg.server.port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    main()
