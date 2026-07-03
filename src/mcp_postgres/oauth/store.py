"""SQLite-backed persistence for the OAuth 2.1 layer.

Holds the OAuth state that must survive a service restart — otherwise a saved
claude.ai connector would have to re-register and re-authorize after every deploy
(``update.py`` restarts the service each time): registered DCR clients, one-time
authorization codes, access tokens, and refresh tokens. In-flight logins are NOT
stored here — they live in memory in the provider, since a restart mid-login just
means the user retries.

Rows are the SDK's own pydantic models serialised to JSON, so the store stays
agnostic to their exact shape. Connections are opened per operation (the file is
local and traffic is tiny), which sidesteps sqlite's same-thread rule under
uvicorn's async workers and keeps every call self-contained.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id  TEXT PRIMARY KEY,
    data       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
    code       TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS access_tokens (
    token      TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    expires_at REAL
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token      TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    expires_at REAL
);
"""


class OAuthStore:
    """A tiny sqlite store for OAuth clients, codes, and tokens."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        existed = os.path.exists(self.path)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()
        if not existed:
            # The file holds client secrets and tokens — lock it down (best effort;
            # a no-op on platforms without POSIX modes).
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # -- registered clients (DCR) ---------------------------------------------

    def put_client(self, client: OAuthClientInformationFull) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO clients (client_id, data) VALUES (?, ?)",
                (client.client_id, client.model_dump_json()),
            )
            conn.commit()

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT data FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate_json(row["data"])

    # -- authorization codes (single-use, short-lived) ------------------------

    def put_auth_code(self, code: AuthorizationCode) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO auth_codes (code, data, expires_at) VALUES (?, ?, ?)",
                (code.code, code.model_dump_json(), code.expires_at),
            )
            conn.commit()

    def take_auth_code(self, code: str) -> AuthorizationCode | None:
        """Load and delete a code atomically — codes are single-use, so even a
        redemption that later fails validation must not leave the code replayable."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT data, expires_at FROM auth_codes WHERE code = ?", (code,)
            ).fetchone()
            conn.execute("DELETE FROM auth_codes WHERE code = ?", (code,))
            conn.commit()
        if row is None or row["expires_at"] < time.time():
            return None
        return AuthorizationCode.model_validate_json(row["data"])

    # -- access tokens --------------------------------------------------------

    def put_access_token(self, tok: AccessToken) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO access_tokens (token, data, expires_at) VALUES (?, ?, ?)",
                (tok.token, tok.model_dump_json(), tok.expires_at),
            )
            conn.commit()

    def get_access_token(self, token: str) -> AccessToken | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT data, expires_at FROM access_tokens WHERE token = ?", (token,)
            ).fetchone()
        if row is None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < time.time():
            self.delete_access_token(token)
            return None
        return AccessToken.model_validate_json(row["data"])

    def delete_access_token(self, token: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM access_tokens WHERE token = ?", (token,))
            conn.commit()

    # -- refresh tokens -------------------------------------------------------

    def put_refresh_token(self, tok: RefreshToken) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO refresh_tokens (token, data, expires_at) VALUES (?, ?, ?)",
                (tok.token, tok.model_dump_json(), tok.expires_at),
            )
            conn.commit()

    def get_refresh_token(self, token: str) -> RefreshToken | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT data, expires_at FROM refresh_tokens WHERE token = ?", (token,)
            ).fetchone()
        if row is None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < time.time():
            self.delete_refresh_token(token)
            return None
        return RefreshToken.model_validate_json(row["data"])

    def delete_refresh_token(self, token: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM refresh_tokens WHERE token = ?", (token,))
            conn.commit()

    # -- maintenance ----------------------------------------------------------

    def prune(self) -> int:
        """Delete expired codes and tokens. Returns the number of rows removed."""
        now = time.time()
        with closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM auth_codes WHERE expires_at < ?", (now,))
            removed = cur.rowcount
            cur = conn.execute(
                "DELETE FROM access_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            removed += cur.rowcount
            cur = conn.execute(
                "DELETE FROM refresh_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            removed += cur.rowcount
            conn.commit()
        return removed
