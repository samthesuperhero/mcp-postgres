#!/usr/bin/env python3
"""Installer for mcp-postgres.

Run once, as root, on the target RHEL-based host:

    sudo mcp-postgres/install --bind 127.0.0.1 --port 8080 --start --run-selftest
    # equivalently: sudo python3 mcp-postgres/install.py ...  (the launcher is a thin wrapper)

Creates the mcp-postgres OS user, lays down all files, builds the venv, writes
config + secrets with correct permissions, installs the systemd unit and a scoped
sudoers drop-in, and optionally starts the service and runs the self-tests.

Stdlib only (no third-party imports) — it must run before the venv exists.
Idempotent: safe to re-run to upgrade or repair.
"""

from __future__ import annotations

import argparse
import getpass
import grp
import os
import pwd
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent

SERVICE_USER = "mcp-postgres"
HOME_DIR = Path("/opt/mcp-postgres")
VENV_DIR = HOME_DIR / "venv"
LIBEXEC_DIR = Path("/usr/libexec/mcp-postgres")
CONFIG_DIR = Path("/etc/mcp-postgres")
UNIT_PATH = Path("/usr/lib/systemd/system/mcp-postgres.service")
SUDOERS_PATH = Path("/etc/sudoers.d/mcp-postgres")


def info(msg: str) -> None:
    print(f"[install] {msg}")


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"[install] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    info("run: " + " ".join(cmd))
    return subprocess.run(cmd, check=True, **kw)


# -- steps -------------------------------------------------------------------


def preflight(args) -> None:
    if os.geteuid() != 0:
        die("must run as root (use sudo)")
    if sys.version_info < (3, 11):
        die(f"Python 3.11+ required, have {sys.version.split()[0]}")
    if shutil.which("systemctl") is None:
        die("systemctl not found — this installer targets systemd/RHEL hosts")
    if shutil.which("useradd") is None:
        die("useradd not found")
    # Best-effort PostgreSQL reachability check (warn only).
    try:
        import socket

        with socket.create_connection(("127.0.0.1", 5432), timeout=2):
            info("PostgreSQL reachable at 127.0.0.1:5432")
    except OSError:
        info("WARNING: PostgreSQL not reachable at 127.0.0.1:5432 (continuing)")


def ensure_user() -> None:
    try:
        pwd.getpwnam(SERVICE_USER)
        info(f"user {SERVICE_USER} already exists")
    except KeyError:
        run(
            [
                "useradd",
                "--system",
                "--home-dir",
                str(HOME_DIR),
                "--shell",
                "/sbin/nologin",
                SERVICE_USER,
            ]
        )
        info(f"created user {SERVICE_USER}")


def grant_wheel() -> None:
    try:
        grp.getgrnam("wheel")
    except KeyError:
        info("WARNING: 'wheel' group not present; skipping --grant-wheel")
        return
    run(["usermod", "-aG", "wheel", SERVICE_USER])
    info(f"added {SERVICE_USER} to wheel")


def _chown(path: Path, user: str = SERVICE_USER, group: str | None = None) -> None:
    pw = pwd.getpwnam(user)
    gid = grp.getgrnam(group).gr_gid if group else pw.pw_gid
    os.chown(path, pw.pw_uid, gid)


def lay_down_files() -> None:
    for d in (HOME_DIR, LIBEXEC_DIR, CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)

    # privhelper -> /usr/libexec, root-owned, executable.
    dst = LIBEXEC_DIR / "privhelper"
    shutil.copy2(REPO / "privhelper.py", dst)
    os.chown(dst, 0, 0)
    os.chmod(dst, 0o755)
    info(f"installed {dst}")

    # systemd unit.
    shutil.copy2(REPO / "packaging" / "mcp-postgres.service", UNIT_PATH)
    os.chmod(UNIT_PATH, 0o644)
    info(f"installed {UNIT_PATH}")

    # sudoers drop-in (validated before install).
    install_sudoers()


def install_sudoers() -> None:
    content = (REPO / "packaging" / "sudoers").read_text(encoding="utf-8")
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sudoers") as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        check = subprocess.run(["visudo", "-cf", tmp_path], capture_output=True)
        if check.returncode != 0:
            die("sudoers validation failed: " + check.stderr.decode(errors="replace"))
        shutil.copy2(tmp_path, SUDOERS_PATH)
        os.chown(SUDOERS_PATH, 0, 0)
        os.chmod(SUDOERS_PATH, 0o440)
        info(f"installed {SUDOERS_PATH}")
    finally:
        os.unlink(tmp_path)


def build_venv(args, *, force_reinstall: bool = False) -> None:
    python = args.python or sys.executable
    if not VENV_DIR.exists():
        run([python, "-m", "venv", str(VENV_DIR)])
    pip = str(VENV_DIR / "bin" / "pip")
    run([pip, "install", "--upgrade", "pip"])
    if args.offline_wheels:
        base = [pip, "install", "--no-index", "--find-links", args.offline_wheels]
        target = "mcp-postgres"
    else:
        base = [pip, "install"]
        target = str(REPO)
    if force_reinstall:
        # The pinned version rarely changes between commits, so a plain re-install
        # would be a no-op ("already satisfied"). Force just the package code first
        # (fast, --no-deps), then a normal install to pull any newly added deps.
        run(base + ["--force-reinstall", "--no-deps", target])
    run(base + [target])
    _chown_tree(HOME_DIR)
    info("venv built and package installed")


def _chown_tree(root: Path) -> None:
    pw = pwd.getpwnam(SERVICE_USER)
    for dirpath, dirnames, filenames in os.walk(root):
        os.chown(dirpath, pw.pw_uid, pw.pw_gid)
        for name in dirnames + filenames:
            try:
                os.chown(os.path.join(dirpath, name), pw.pw_uid, pw.pw_gid)
            except FileNotFoundError:
                pass


def write_config(args) -> None:
    cfg = CONFIG_DIR / "config.toml"
    if cfg.exists() and not args.force:
        info(f"{cfg} exists — keeping (use --force to regenerate from flags)")
        return
    template = (REPO / "packaging" / "config.toml.template").read_text(encoding="utf-8")
    rendered = (
        template.replace("__BIND__", args.bind)
        .replace("__PORT__", str(args.port))
        .replace("__PATH__", args.path)
        .replace("__DBUSER__", args.db_user)
        .replace("__DBNAME__", args.db_name)
        .replace("__LOGLEVEL__", args.log_level)
    )
    cfg.write_text(rendered, encoding="utf-8")
    _chown(cfg, "root", SERVICE_USER)
    os.chmod(cfg, 0o640)
    info(f"wrote {cfg}")


def write_secret(name: str, value: str) -> None:
    path = CONFIG_DIR / name
    # Write restrictively from the start.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value.strip() + "\n")
    _chown(path, SERVICE_USER)
    os.chmod(path, 0o600)
    info(f"wrote {path} (0600)")


def _read_existing(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _prompt_password(db_user: str) -> str:
    while True:
        p1 = getpass.getpass(f"Password for PostgreSQL role '{db_user}': ")
        p2 = getpass.getpass("Confirm password: ")
        if p1 and p1 == p2:
            return p1
        print("passwords empty or did not match; try again", file=sys.stderr)


def resolve_db_password(args) -> tuple[str, bool]:
    """Return (password, should_write).

    An existing secret file is preserved (and read, so --create-db-role still
    works) unless --force or an explicit MCP_PG_DB_PASSWORD is provided.
    """
    path = CONFIG_DIR / "secret"
    env = os.environ.get("MCP_PG_DB_PASSWORD")
    if env:
        return env, True
    if path.exists() and not args.force:
        info(f"{path} exists — keeping existing DB password (use --force to change)")
        return _read_existing(path), False
    if args.non_interactive:
        die("no DB password: set MCP_PG_DB_PASSWORD or run without --non-interactive")
    return _prompt_password(args.db_user), True


def resolve_token(args) -> tuple[str, bool, bool]:
    """Return (token, should_write, generated).

    An existing token file is preserved unless --force or an explicit
    MCP_PG_TOKEN is provided; otherwise a fresh token is generated.
    """
    path = CONFIG_DIR / "token"
    env = os.environ.get("MCP_PG_TOKEN")
    if env:
        return env, True, False
    if path.exists() and not args.force:
        info(f"{path} exists — keeping existing bearer token (use --force to rotate)")
        return _read_existing(path), False, False
    return secrets.token_hex(32), True, True


def create_db_role(args, password: str) -> None:
    if shutil.which("psql") is None:
        info("WARNING: psql not found; skipping --create-db-role")
        return
    ident = args.db_user
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ident):
        die(f"unsafe role name for --create-db-role: {ident!r}")
    escaped_pw = password.replace("'", "''")
    sql = f"CREATE ROLE {ident} LOGIN PASSWORD '{escaped_pw}';"
    proc = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        info(f"WARNING: could not create role {ident}: {err}")
    else:
        info(f"created PostgreSQL role {ident}")


def activate(args, token: str, generated_token: bool) -> None:
    run(["systemctl", "daemon-reload"])
    if args.start:
        run(["systemctl", "enable", "--now", "mcp-postgres"])
        info("service enabled and started")
    if args.run_selftest:
        selftest = VENV_DIR / "bin" / "mcp-postgres-selftest"
        info("running self-tests...")
        subprocess.run(["sudo", "-u", SERVICE_USER, str(selftest)])
    print()
    info("done.")
    if generated_token:
        print()
        print("  >>> Generated MCP bearer token (save this — agents must present it):")
        print(f"      {token}")
        print()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Install mcp-postgres")
    p.add_argument("--bind", default="127.0.0.1", help="MCP bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8080, help="MCP port (default 8080)")
    p.add_argument("--path", default="/mcp", help="MCP HTTP path (default /mcp)")
    p.add_argument("--db-user", default="mcp", help="PostgreSQL role (default mcp)")
    p.add_argument("--db-name", default="postgres", help="default database (default postgres)")
    p.add_argument("--log-level", default="INFO", help="log level (default INFO)")
    p.add_argument("--python", help="python interpreter to build the venv with")
    p.add_argument("--offline-wheels", help="dir of wheels for offline install")
    p.add_argument("--grant-wheel", action="store_true", help="add mcp-postgres to the wheel group")
    p.add_argument("--create-db-role", action="store_true", help="create the PostgreSQL role via postgres superuser")
    p.add_argument("--start", action="store_true", help="enable and start the service")
    p.add_argument("--run-selftest", action="store_true", help="run self-tests after install")
    p.add_argument("--non-interactive", action="store_true", help="never prompt (secrets via env)")
    p.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing config.toml and regenerate/rotate secrets",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    preflight(args)

    password, write_pw = resolve_db_password(args)
    token, write_token, token_generated = resolve_token(args)

    ensure_user()
    if args.grant_wheel:
        grant_wheel()
    lay_down_files()
    build_venv(args)
    write_config(args)
    if write_pw:
        write_secret("secret", password)
    if write_token:
        write_secret("token", token)
    _chown(CONFIG_DIR)
    os.chmod(CONFIG_DIR, 0o750)

    if args.create_db_role:
        create_db_role(args, password)

    activate(args, token, token_generated and write_token)


if __name__ == "__main__":
    main()
