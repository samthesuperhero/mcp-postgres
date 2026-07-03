"""Offline unit tests for the OAuth provider and the full HTTP grant flow.

Two layers:

- Direct provider-method tests (dual auth, code single-use, refresh rotation),
  driven through ``anyio.run`` so no pytest-asyncio dependency is needed — the
  same pattern the self-test uses.
- An in-process end-to-end test that drives the SDK's real HTTP handlers over an
  ASGI transport: metadata → dynamic client registration → /authorize → /login
  (token as passphrase) → /token (PKCE) → refresh, plus the unauthenticated
  ``/mcp`` 401 + ``WWW-Authenticate`` challenge. This is exactly the path a
  claude.ai web connector walks.
"""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
from mcp.server.auth.provider import AuthorizationParams
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthClientInformationFull

from mcp_postgres.oauth import OAuthStore, PostgresOAuthProvider
from mcp_postgres.oauth.login import register_login_routes

TOKEN = "the-static-token"


def _provider(tmp_path):
    store = OAuthStore(tmp_path / "oauth.db")
    return PostgresOAuthProvider(
        store, static_token=TOKEN, access_token_ttl=3600, refresh_token_ttl=99999
    )


def _client():
    return OAuthClientInformationFull(
        client_id="client-1",
        client_secret="sekret",
        redirect_uris=["http://localhost/callback"],
        grant_types=["authorization_code", "refresh_token"],
    )


# -- dual auth ----------------------------------------------------------------


def test_static_token_accepted(tmp_path):
    prov = _provider(tmp_path)
    tok = anyio.run(prov.load_access_token, TOKEN)
    assert tok is not None
    assert tok.client_id == "static-token"
    assert tok.expires_at is None  # never expires


def test_wrong_and_unknown_tokens_rejected(tmp_path):
    prov = _provider(tmp_path)
    assert anyio.run(prov.load_access_token, "wrong") is None
    assert anyio.run(prov.load_access_token, "") is None


def test_no_static_token_configured(tmp_path):
    store = OAuthStore(tmp_path / "oauth.db")
    prov = PostgresOAuthProvider(store, static_token="")
    # An empty configured token must never authenticate an empty presented token.
    assert anyio.run(prov.load_access_token, "") is None


# -- authorize / login gate ---------------------------------------------------


def _authorize(prov, client):
    params = AuthorizationParams(
        state="st-1",
        scopes=None,
        code_challenge="challenge",
        redirect_uri="http://localhost/callback",
        redirect_uri_provided_explicitly=True,
        resource="http://127.0.0.1/mcp",
    )
    url = anyio.run(prov.authorize, client, params)
    return parse_qs(urlparse(url).query)["rid"][0]


def test_authorize_parks_pending_and_completes(tmp_path):
    prov = _provider(tmp_path)
    client = _client()
    rid = _authorize(prov, client)
    assert prov.pending_client(rid) is client

    redirect = prov.complete_authorization(rid)
    q = parse_qs(urlparse(redirect).query)
    assert q["state"] == ["st-1"]
    code = q["code"][0]
    # The code was persisted and is redeemable exactly once.
    assert anyio.run(prov.load_authorization_code, client, code) is not None
    assert anyio.run(prov.load_authorization_code, client, code) is None
    # And the pending entry was consumed.
    assert prov.pending_client(rid) is None


def test_unknown_rid_yields_no_client_or_code(tmp_path):
    prov = _provider(tmp_path)
    assert prov.pending_client("bogus") is None
    assert prov.complete_authorization("bogus") is None


# -- token issuance & refresh rotation ----------------------------------------


def test_exchange_code_then_refresh_rotates(tmp_path):
    prov = _provider(tmp_path)
    client = _client()
    rid = _authorize(prov, client)
    code_url = prov.complete_authorization(rid)
    code = parse_qs(urlparse(code_url).query)["code"][0]

    auth_code = anyio.run(prov.load_authorization_code, client, code)
    tokens = anyio.run(prov.exchange_authorization_code, client, auth_code)
    assert tokens.access_token and tokens.refresh_token
    assert anyio.run(prov.load_access_token, tokens.access_token) is not None

    # Refresh rotates both tokens: the old refresh token is invalidated.
    async def _refresh():
        rt = await prov.load_refresh_token(client, tokens.refresh_token)
        new = await prov.exchange_refresh_token(client, rt, rt.scopes)
        return new, await prov.load_refresh_token(client, tokens.refresh_token)

    new_tokens, old_after = anyio.run(_refresh)
    assert new_tokens.access_token and new_tokens.refresh_token
    assert new_tokens.refresh_token != tokens.refresh_token
    assert old_after is None  # old refresh token no longer loadable


# -- full HTTP flow over an ASGI transport ------------------------------------


def _build_app(tmp_path):
    prov = _provider(tmp_path)
    auth = AuthSettings(
        issuer_url="http://127.0.0.1:41780",
        resource_server_url="http://127.0.0.1:41780/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
    mcp = FastMCP(
        "mcp-postgres",
        host="127.0.0.1",
        port=41780,
        streamable_http_path="/mcp",
        auth_server_provider=prov,
        auth=auth,
    )
    register_login_routes(mcp, prov, TOKEN)
    return mcp.streamable_http_app()


async def _run_full_flow(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:41780"
    ) as c:
        # discovery
        md = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert md["registration_endpoint"].endswith("/register")
        prm = (await c.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert prm["authorization_servers"]

        # dynamic client registration
        reg = await c.post(
            "/register",
            json={"redirect_uris": ["http://localhost/callback"], "client_name": "t"},
        )
        assert reg.status_code == 201, reg.text
        client = reg.json()
        cid, csec = client["client_id"], client["client_secret"]

        # authorize with PKCE
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        az = await c.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": "http://localhost/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "st-123",
            },
        )
        assert az.status_code == 302
        rid = parse_qs(urlparse(az.headers["location"]).query)["rid"][0]

        # login page then wrong / right passphrase
        assert "passphrase" in (await c.get("/login", params={"rid": rid})).text
        assert (
            await c.post("/login", data={"rid": rid, "passphrase": "nope"})
        ).status_code == 401
        ok = await c.post("/login", data={"rid": rid, "passphrase": TOKEN})
        assert ok.status_code == 302
        q = parse_qs(urlparse(ok.headers["location"]).query)
        assert q["state"] == ["st-123"]
        code = q["code"][0]

        # token exchange
        tok = await c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://localhost/callback",
                "client_id": cid,
                "client_secret": csec,
                "code_verifier": verifier,
            },
        )
        assert tok.status_code == 200, tok.text
        access = tok.json()["access_token"]
        assert access

        # code is single-use
        reuse = await c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://localhost/callback",
                "client_id": cid,
                "client_secret": csec,
                "code_verifier": verifier,
            },
        )
        assert reuse.status_code == 400

        # unauthenticated /mcp challenges with resource metadata
        r = await c.get("/mcp")
        assert r.status_code == 401
        assert "resource_metadata" in r.headers.get("www-authenticate", "")


def test_full_http_oauth_flow(tmp_path):
    anyio.run(_run_full_flow, _build_app(tmp_path))
