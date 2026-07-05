#!/usr/bin/env python3
"""Run the live prod-DB test suite on a deployed mcp-postgres host.

Run as root from the repo directory (same place as install.py):

    sudo mcp-postgres/run-tests                  # full tests/prod suite, as the service user
    sudo mcp-postgres/run-tests -k "not write"   # read-only subset (never writes to the DB)
    sudo mcp-postgres/run-tests -v               # any extra args are forwarded to pytest
    sudo mcp-postgres/run-tests --root           # run in place as root, not as mcp-postgres

Where `mcp-postgres-selftest` runs a handful of on-deploy checks, this drives the
whole pytest suite under `tests/prod/`, most of which does real work against the
*live* database and the *running* service (see `tests/prod/test_live_*.py`). Tests
whose resource isn't reachable skip themselves, so it is safe to run on a partial
environment.

It handles the three things that otherwise make running these on the host awkward:

  * pytest isn't in the prod venv (the installer does `pip install <repo>`, not the
    `test` extra) -- it is added here, idempotently.
  * the DB password and bearer token are mode 0600, owned by mcp-postgres, so the
    tests must run AS mcp-postgres (or root) to read them -- a plain admin login
    would make every live test skip. By default the suite runs as the service user,
    exactly as the service does, for a faithful read of what it sees.
  * the tests live in the git checkout, which the mcp-postgres user often cannot
    traverse (a 0700 home). Rather than touch the checkout's permissions, a small
    copy it owns is staged in a temp dir.

Stdlib only -- it must run without the application venv. Reuses install.py's
constants and helpers, exactly as update.py does.
"""

from __future__ import annotations

import argparse
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Reuse the installer's constants and helpers (SERVICE_USER, VENV_DIR, REPO,
# _chown_tree), as update.py / admin.py do.
import install

PYTEST_SPEC = "pytest>=8.0"  # matches pyproject's [project.optional-dependencies] test
TEST_PATH = "tests/prod"


def info(msg: str) -> None:
    print(f"[run-tests] {msg}")


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"[run-tests] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def preflight() -> None:
    if os.geteuid() != 0:
        die("must run as root (use sudo)")
    try:
        pwd.getpwnam(install.SERVICE_USER)
    except KeyError:
        die(f"service user {install.SERVICE_USER!r} not found -- run install.py first")
    if not install.VENV_DIR.exists():
        die(f"venv not found at {install.VENV_DIR} -- run install.py first")


def ensure_pytest() -> Path:
    """Make sure pytest is in the prod venv; return the venv's python path."""
    if not (install.VENV_DIR / "bin" / "pytest").exists():
        info(f"pytest not in the venv -- installing {PYTEST_SPEC}")
        pip = str(install.VENV_DIR / "bin" / "pip")
        proc = subprocess.run([pip, "install", PYTEST_SPEC])
        if proc.returncode != 0:
            die("failed to install pytest into the venv")
        install._chown_tree(install.VENV_DIR)  # keep the venv owned by the service user
    return install.VENV_DIR / "bin" / "python"


def stage_workspace() -> str:
    """Copy the checkout into a temp dir the service user owns, so pytest can read it.

    The mcp-postgres user often can't traverse the checkout (a 0700 home), so rather
    than change its permissions we hand it a private copy. The whole tree is copied
    (minus VCS/build/cache noise) so every repo-relative path a test resolves is
    present -- e.g. `packaging/config.toml.template` and the root `privhelper.py`.
    `mcp_postgres` and `pytest` still import from the venv, not from this copy.
    """
    ws = tempfile.mkdtemp(prefix="mcp-postgres-tests-")
    shutil.copytree(
        install.REPO,
        ws,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", ".claude", "__pycache__", "*.pyc", "*.egg-info",
            ".pytest_cache", "build", "dist",
        ),
    )
    install._chown_tree(Path(ws))  # hand the whole workspace to the service user
    return ws


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    p = argparse.ArgumentParser(
        description="Run the mcp-postgres live prod-DB test suite on the host.",
        epilog="Any additional arguments are forwarded to pytest (e.g. -v, -k 'not write').",
    )
    p.add_argument(
        "--root",
        action="store_true",
        help="run in place as root instead of as the mcp-postgres service user "
        "(simpler where the checkout is root-readable; the reported OS tier then "
        "reflects root, not the service)",
    )
    return p.parse_known_args()


def main() -> None:
    args, pytest_args = parse_args()
    preflight()
    py = ensure_pytest()

    if args.root:
        cmd = [str(py), "-m", "pytest", TEST_PATH, *pytest_args]
        info("run (as root): " + " ".join(cmd))
        sys.exit(subprocess.run(cmd, cwd=str(install.REPO)).returncode)

    ws = stage_workspace()
    try:
        cmd = [
            "sudo", "-u", install.SERVICE_USER,
            str(py), "-m", "pytest", f"{ws}/{TEST_PATH}", *pytest_args,
        ]
        info(f"run (as {install.SERVICE_USER}): " + " ".join(cmd))
        rc = subprocess.run(cmd, cwd=ws).returncode
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    sys.exit(rc)


if __name__ == "__main__":
    main()
