"""OAuth 2.1 authorization layer for mcp-postgres.

Opt-in (``[oauth] enabled`` in config.toml). When on, the server fronts ``/mcp``
with a standards-compliant OAuth 2.1 Authorization Server + Resource Server —
dynamic client registration (RFC 7591) and the authorization-code + PKCE flow —
so browser clients such as the claude.ai web connector can attach. The static
bearer token keeps working alongside it (dual auth).

The heavy lifting (metadata endpoints, DCR validation, PKCE verification,
redirect_uri matching, code expiry, client authentication) is done by the MCP
SDK's ``mcp.server.auth`` route handlers. This package supplies only the pieces
the SDK delegates to the deployment:

- ``store``    — sqlite persistence that survives service restarts.
- ``provider`` — the ``OAuthAuthorizationServerProvider`` implementation.
- ``login``    — the passphrase-gated approval page for the ``/authorize`` step.
"""

from .provider import PostgresOAuthProvider
from .store import OAuthStore

__all__ = ["OAuthStore", "PostgresOAuthProvider"]
