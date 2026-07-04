#!/usr/bin/env python3
"""Privileged helper for mcp-postgres — the ONLY command the service runs via sudo.

Hard-enforces the two-file allowlist (postgresql.conf, pg_hba.conf) at the OS
boundary, independently of the calling service. Installed root-owned at
/usr/libexec/mcp-postgres/privhelper and exposed to the mcp-postgres user through
a scoped NOPASSWD sudoers rule.

Subcommands:
    --check          exit 0 (used to probe whether sudo access exists)
    read  <path>     print an allowlisted file's contents
    write <path>     replace an allowlisted file from stdin (timestamped .bak first)
    reload           systemctl reload postgresql

Stdlib only — it runs before/without the application venv.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

ALLOWED_BASENAMES = frozenset({"postgresql.conf", "pg_hba.conf"})
PG_SERVICE = os.environ.get("MCP_PG_SERVICE", "postgresql")


def die(msg: str, code: int = 2) -> "NoReturn":  # type: ignore[name-defined]
    sys.stderr.write(f"privhelper: {msg}\n")
    sys.exit(code)


def resolve_allowed(path: str, must_exist: bool = True) -> str:
    """Canonicalise a path and confirm its basename is on the allowlist."""
    # Require an absolute path up front. A relative one would be resolved against
    # this process's CWD (``/`` under sudo), silently producing e.g.
    # ``/postgresql.conf`` — fail loudly instead so a caller bug is obvious.
    if not os.path.isabs(path):
        die(f"refusing {path!r}: expected an absolute path")
    real = os.path.realpath(path)
    if os.path.basename(real) not in ALLOWED_BASENAMES:
        die(f"refusing {real!r}: basename not in allowlist {sorted(ALLOWED_BASENAMES)}")
    if must_exist:
        if not os.path.exists(real):
            die(f"refusing {real!r}: file does not exist")
        if not os.path.isfile(real):
            die(f"refusing {real!r}: not a regular file")
    return real


def cmd_read(path: str) -> None:
    real = resolve_allowed(path)
    with open(real, "rb") as fh:
        sys.stdout.buffer.write(fh.read())


def cmd_write(path: str) -> None:
    real = resolve_allowed(path)
    data = sys.stdin.buffer.read()

    st = os.stat(real)
    directory = os.path.dirname(real)

    # Timestamped backup next to the original.
    backup = f"{real}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(real, backup)

    # Atomic replace: write a temp file in the same dir, match perms/owner, rename.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mcp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        shutil.copystat(real, tmp)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass
        os.replace(tmp, real)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    sys.stderr.write(f"privhelper: wrote {real} (backup {backup})\n")


def cmd_reload() -> None:
    proc = subprocess.run(
        ["systemctl", "reload", PG_SERVICE], capture_output=True
    )
    if proc.returncode != 0:
        die(proc.stderr.decode(errors="replace").strip() or "reload failed", code=proc.returncode)
    sys.stderr.write(f"privhelper: reloaded {PG_SERVICE}\n")


def main() -> None:
    parser = argparse.ArgumentParser(prog="privhelper", add_help=True)
    parser.add_argument("--check", action="store_true", help="exit 0 (sudo capability probe)")
    sub = parser.add_subparsers(dest="command")
    p_read = sub.add_parser("read")
    p_read.add_argument("path")
    p_write = sub.add_parser("write")
    p_write.add_argument("path")
    sub.add_parser("reload")

    args = parser.parse_args()

    if args.check:
        sys.exit(0)
    if args.command == "read":
        cmd_read(args.path)
    elif args.command == "write":
        cmd_write(args.path)
    elif args.command == "reload":
        cmd_reload()
    else:
        parser.print_help(sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
