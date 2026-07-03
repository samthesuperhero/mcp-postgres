"""Offline unit tests for the OAuth sqlite store.

Pure storage tests — no DB, no live service (mirrors test_confedit /
test_configmigrate). Covers round-trips, single-use codes, expiry, pruning, and
persistence across a reopen (the property that lets a claude.ai connector survive
a service restart).
"""

import os
import time

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

from mcp_postgres.oauth.store import OAuthStore


def _client(cid="client-1"):
    return OAuthClientInformationFull(
        client_id=cid,
        client_secret="sekret",
        redirect_uris=["http://localhost/callback"],
        client_name="test",
    )


def _code(code="code-1", expires_in=300.0):
    return AuthorizationCode(
        code=code,
        scopes=[],
        expires_at=time.time() + expires_in,
        client_id="client-1",
        code_challenge="challenge",
        redirect_uri="http://localhost/callback",
        redirect_uri_provided_explicitly=True,
        subject="owner",
    )


def _access(token="at-1", expires_in=3600):
    return AccessToken(
        token=token,
        client_id="client-1",
        scopes=[],
        expires_at=int(time.time()) + expires_in,
        subject="owner",
    )


def _refresh(token="rt-1", expires_in=99999):
    return RefreshToken(
        token=token,
        client_id="client-1",
        scopes=[],
        expires_at=int(time.time()) + expires_in,
        subject="owner",
    )


# -- clients ------------------------------------------------------------------


def test_client_round_trip(tmp_path):
    store = OAuthStore(tmp_path / "oauth.db")
    store.put_client(_client())
    got = store.get_client("client-1")
    assert got is not None
    assert got.client_id == "client-1"
    assert got.client_secret == "sekret"
    assert str(got.redirect_uris[0]) == "http://localhost/callback"


def test_missing_client_is_none(tmp_path):
    store = OAuthStore(tmp_path / "oauth.db")
    assert store.get_client("nope") is None


def test_clients_persist_across_reopen(tmp_path):
    path = tmp_path / "oauth.db"
    OAuthStore(path).put_client(_client("persisted"))
    # A fresh store object on the same file (as after a service restart) sees it.
    assert OAuthStore(path).get_client("persisted") is not None


# -- authorization codes ------------------------------------------------------


def test_auth_code_is_single_use(tmp_path):
    store = OAuthStore(tmp_path / "oauth.db")
    store.put_auth_code(_code())
    assert store.take_auth_code("code-1") is not None
    assert store.take_auth_code("code-1") is None  # consumed on first take


def test_expired_auth_code_not_returned(tmp_path):
    store = OAuthStore(tmp_path / "oauth.db")
    store.put_auth_code(_code(expires_in=-1))
    assert store.take_auth_code("code-1") is None


# -- access & refresh tokens --------------------------------------------------


def test_access_token_round_trip_and_delete(tmp_path):
    store = OAuthStore(tmp_path / "oauth.db")
    store.put_access_token(_access())
    assert store.get_access_token("at-1") is not None
    store.delete_access_token("at-1")
    assert store.get_access_token("at-1") is None


def test_expired_access_token_not_returned(tmp_path):
    store = OAuthStore(tmp_path / "oauth.db")
    store.put_access_token(_access(expires_in=-10))
    assert store.get_access_token("at-1") is None


def test_refresh_token_round_trip_and_delete(tmp_path):
    store = OAuthStore(tmp_path / "oauth.db")
    store.put_refresh_token(_refresh())
    assert store.get_refresh_token("rt-1") is not None
    store.delete_refresh_token("rt-1")
    assert store.get_refresh_token("rt-1") is None


# -- pruning ------------------------------------------------------------------


def test_prune_removes_only_expired(tmp_path):
    store = OAuthStore(tmp_path / "oauth.db")
    store.put_access_token(_access("live", expires_in=3600))
    store.put_access_token(_access("dead", expires_in=-10))
    store.put_refresh_token(_refresh("live-r", expires_in=3600))
    store.put_refresh_token(_refresh("dead-r", expires_in=-10))
    store.put_auth_code(_code("dead-c", expires_in=-10))

    removed = store.prune()
    assert removed == 3
    assert store.get_access_token("live") is not None
    assert store.get_refresh_token("live-r") is not None


# -- file permissions (POSIX only) --------------------------------------------


def test_store_file_is_locked_down(tmp_path):
    path = tmp_path / "oauth.db"
    OAuthStore(path)
    if os.name == "posix":
        assert (os.stat(path).st_mode & 0o777) == 0o600
