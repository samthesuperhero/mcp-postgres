"""Client wrapper around the privileged helper (``privhelper``).

The service (running as ``mcp-postgres``) never touches system files directly.
It shells out to ``sudo -n <privhelper> ...``; whether that succeeds depends on
the OS-level rights granted to the ``mcp-postgres`` user (wheel or the scoped
sudoers drop-in). The helper itself hard-enforces the two-file allowlist.
"""

from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)

PRIVHELPER = os.environ.get("MCP_PG_PRIVHELPER", "/usr/libexec/mcp-postgres/privhelper")

# App-layer half of the two-layer allowlist: refuse a disallowed target before
# shelling out to sudo. The privhelper hard-enforces the same set at the OS
# boundary regardless, but failing here gives a clearer error and never spawns a
# privileged process for a path that could never be accepted.
ALLOWED_BASENAMES = frozenset({"postgresql.conf", "pg_hba.conf"})


class PrivError(RuntimeError):
    pass


class PrivClient:
    def __init__(self, helper: str = PRIVHELPER):
        self.helper = helper

    def _run(self, args: list[str], input_bytes: bytes | None = None, check: bool = True):
        cmd = ["sudo", "-n", self.helper, *args]
        try:
            proc = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=30)
        except FileNotFoundError as exc:
            raise PrivError(f"sudo/privhelper not available: {exc}") from exc
        if check and proc.returncode != 0:
            msg = proc.stderr.decode(errors="replace").strip() or f"exit {proc.returncode}"
            raise PrivError(msg)
        return proc

    def _check_path(self, path: str) -> None:
        if os.path.basename(path) not in ALLOWED_BASENAMES:
            raise PrivError(f"refusing {path!r}: basename not allowlisted")

    def check(self) -> bool:
        """Return True iff we can invoke the privhelper via passwordless sudo."""
        try:
            return self._run(["--check"], check=False).returncode == 0
        except PrivError:
            return False

    def read(self, path: str) -> str:
        self._check_path(path)
        return self._run(["read", path]).stdout.decode(errors="replace")

    def write(self, path: str, content: str) -> None:
        self._check_path(path)
        self._run(["write", path], input_bytes=content.encode())

    def reload(self) -> None:
        self._run(["reload"])
