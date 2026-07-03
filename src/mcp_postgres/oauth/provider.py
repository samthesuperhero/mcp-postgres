"""OAuth 2.1 Authorization Server provider for mcp-postgres.

Implements the MCP SDK's ``OAuthAuthorizationServerProvider`` protocol against the
sqlite :class:`~mcp_postgres.oauth.store.OAuthStore`. The SDK's route handlers do
all the protocol-level work (metadata, DCR validation, PKCE/S256 verification,
redirect_uri matching, code expiry, client authentication); this class only
persists state, mints opaque tokens, and hands the ``/authorize`` step off to a
passphrase-gated login page (see :mod:`~mcp_postgres.oauth.login`).

**Dual auth.** ``load_access_token`` also accepts the static bearer token from
``/etc/mcp-postgres/token``, returning a synthetic full-scope grant, so existing
Claude Code / Claude Desktop / self-test clients keep working when OAuth is on.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .store import OAuthStore

AUTH_CODE_TTL = 300  # seconds a freshly minted code stays redeemable
PENDING_TTL = 600  # seconds an unfinished login (browser at the passphrase page) stays valid
STATIC_CLIENT_ID = "static-token"  # synthetic client_id for the legacy bearer path
SUBJECT = "owner"  # single-tenant: there is exactly one resource owner


@dataclass
class _Pending:
    """An authorization request parked while the browser is at the login page."""

    client: OAuthClientInformationFull
    params: AuthorizationParams
    created_at: float


class PostgresOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(
        self,
        store: OAuthStore,
        *,
        static_token: str = "",
        access_token_ttl: int = 3600,
        refresh_token_ttl: int = 2592000,
        scopes: list[str] | None = None,
    ):
        self.store = store
        self.static_token = static_token
        self.access_token_ttl = access_token_ttl
        self.refresh_token_ttl = refresh_token_ttl
        self.scopes = scopes or []
        self._pending: dict[str, _Pending] = {}

    # -- dynamic client registration (RFC 7591) -------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.put_client(client_info)

    # -- /authorize → login gate ----------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the (already-validated) request and send the browser to the login
        page. A relative URL is returned so the browser resolves it against the
        public origin — the provider never needs to know ``public_url``."""
        self._prune_pending()
        rid = secrets.token_urlsafe(32)
        self._pending[rid] = _Pending(client=client, params=params, created_at=time.time())
        return f"/login?rid={rid}"

    def pending_client(self, rid: str) -> OAuthClientInformationFull | None:
        """The client behind a pending login, or None if the id is unknown/expired.
        Used by the login page to validate ``rid`` before showing the form."""
        self._prune_pending()
        p = self._pending.get(rid)
        return p.client if p else None

    def complete_authorization(self, rid: str) -> str | None:
        """Mint a single-use auth code for a pending request and return the client
        redirect URL (carrying ``code`` and ``state``). Called by the login POST
        once the passphrase checks out. Returns None if the request has expired."""
        self._prune_pending()
        p = self._pending.pop(rid, None)
        if p is None:
            return None
        code = secrets.token_urlsafe(32)  # ~256 bits of entropy (spec floor: 128)
        self.store.put_auth_code(
            AuthorizationCode(
                code=code,
                scopes=p.params.scopes or self.scopes,
                expires_at=time.time() + AUTH_CODE_TTL,
                client_id=p.client.client_id,
                code_challenge=p.params.code_challenge,
                redirect_uri=p.params.redirect_uri,
                redirect_uri_provided_explicitly=p.params.redirect_uri_provided_explicitly,
                resource=p.params.resource,
                subject=SUBJECT,
            )
        )
        return construct_redirect_uri(
            str(p.params.redirect_uri), code=code, state=p.params.state
        )

    def _prune_pending(self) -> None:
        cutoff = time.time() - PENDING_TTL
        for rid in [r for r, p in self._pending.items() if p.created_at < cutoff]:
            del self._pending[rid]

    # -- /token: authorization_code + refresh_token grants --------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        # Single-use: loading consumes it (the SDK handler validates the returned
        # object, then calls exchange_authorization_code with it).
        return self.store.take_auth_code(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        return self._issue(client, authorization_code.scopes, authorization_code.subject)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return self.store.get_refresh_token(refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate both tokens: invalidate the presented refresh token before issuing.
        self.store.delete_refresh_token(refresh_token.token)
        return self._issue(client, scopes or refresh_token.scopes, refresh_token.subject)

    def _issue(
        self, client: OAuthClientInformationFull, scopes: list[str], subject: str | None
    ) -> OAuthToken:
        now = time.time()
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self.store.put_access_token(
            AccessToken(
                token=access,
                client_id=client.client_id,
                scopes=list(scopes),
                expires_at=int(now + self.access_token_ttl),
                subject=subject,
            )
        )
        self.store.put_refresh_token(
            RefreshToken(
                token=refresh,
                client_id=client.client_id,
                scopes=list(scopes),
                expires_at=int(now + self.refresh_token_ttl),
                subject=subject,
            )
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.access_token_ttl,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh,
        )

    # -- resource-server verification (dual auth) -----------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Verify a presented bearer token. Accepts either an OAuth-issued access
        token (from the store) or the static configured token (dual auth)."""
        if self.static_token and hmac.compare_digest(token, self.static_token):
            return AccessToken(
                token=token,
                client_id=STATIC_CLIENT_ID,
                scopes=self.scopes,
                expires_at=None,  # the static token does not expire
                subject=SUBJECT,
            )
        return self.store.get_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # The static token has no stored rows (and its client_id never matches a
        # DCR client, so the revoke handler won't even route it here). Delete from
        # both tables by the token string to cover access and refresh alike.
        self.store.delete_access_token(token.token)
        self.store.delete_refresh_token(token.token)
