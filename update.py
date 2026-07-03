#!/usr/bin/env python3
"""Lightweight in-place update for a deployed mcp-postgres host.

Run as root from the repo directory (same place as install.py), once new code is
available upstream:

    sudo mcp-postgres/update                 # git pull, reinstall, restart, self-test
    sudo mcp-postgres/update --no-pull       # deploy local changes (skip git pull)
    sudo mcp-postgres/update --no-selftest   # skip the post-restart self-test

It reuses install.py's building blocks (files + venv) and adds the two things a
code update needs that a first-time install does not: a *forced* venv reinstall
(the pinned version rarely changes between commits, so pip would otherwise skip
it) and a service restart. Config and secrets under /etc/mcp-postgres are never
touched.

Stdlib only -- it must run without the application venv.

Caveat: this script imports install.py at startup, so changes to the installer
scripts themselves (update.py / install.py) take effect on the NEXT run; ordinary
application code is updated on this run.
"""

from __future__ import annotations

import argparse
import os
import pwd
import shutil
import subprocess
import sys

# Reuse the installer's constants and its file/venv steps (as admin.py does).
import install

SERVICE = "mcp-postgres"


def info(msg: str) -> None:
    print(f"[update] {msg}")


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"[update] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd: list[str], *, check: bool = True, as_user: str | None = None) -> subprocess.CompletedProcess:
    if as_user:
        cmd = ["sudo", "-u", as_user, *cmd]
    info("run: " + " ".join(cmd))
    proc = subprocess.run(cmd)
    if check and proc.returncode != 0:
        die(f"command failed ({proc.returncode}): {' '.join(cmd)}")
    return proc


# -- steps -------------------------------------------------------------------


def preflight() -> None:
    if os.geteuid() != 0:
        die("must run as root (use sudo)")
    if sys.version_info < (3, 11):
        die(f"Python 3.11+ required, have {sys.version.split()[0]}")
    try:
        pwd.getpwnam(install.SERVICE_USER)
    except KeyError:
        die(f"service user {install.SERVICE_USER!r} not found -- run install.py first")
    if not install.VENV_DIR.exists():
        die(f"venv not found at {install.VENV_DIR} -- run install.py first")
    if shutil.which("systemctl") is None:
        die("systemctl not found")


def _head() -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(install.REPO), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def git_pull() -> None:
    if shutil.which("git") is None:
        die("git not found -- pull the code manually and re-run with --no-pull")
    if not (install.REPO / ".git").exists():
        die(f"{install.REPO} is not a git checkout -- update the code manually and use --no-pull")
    # Run as the checkout owner: git refuses a root pull into a user-owned tree
    # ("detected dubious ownership"), and this avoids rewriting file ownership.
    owner = pwd.getpwuid(os.stat(install.REPO).st_uid).pw_name
    before = _head()
    run(["git", "-C", str(install.REPO), "pull", "--ff-only"], as_user=owner)
    after = _head()
    if before and after and before != after:
        info(f"updated {before} -> {after}")
    else:
        info(f"already up to date at {after or 'unknown'}")


def run_selftest() -> None:
    selftest = install.VENV_DIR / "bin" / "mcp-postgres-selftest"
    info("running self-tests...")
    proc = subprocess.run(["sudo", "-u", install.SERVICE_USER, str(selftest)])
    if proc.returncode != 0:
        die("self-test FAILED after update -- the service may be unhealthy", proc.returncode)
    info("self-test passed")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Update a deployed mcp-postgres in place")
    p.add_argument("--no-pull", action="store_true", help="skip git pull (deploy local changes)")
    p.add_argument("--no-selftest", action="store_true", help="skip the post-restart self-test")
    p.add_argument("--python", help="python interpreter to (re)build the venv with")
    p.add_argument("--offline-wheels", help="dir of wheels for offline install")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    preflight()

    if args.no_pull:
        info("skipping git pull (--no-pull)")
    else:
        git_pull()

    install.lay_down_files()  # refresh privhelper, systemd unit, sudoers drop-in
    install.build_venv(args, force_reinstall=True)

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "restart", SERVICE])
    info(f"{SERVICE} restarted")

    if args.no_selftest:
        info("skipping self-test (--no-selftest)")
    else:
        run_selftest()

    print()
    info("update complete.")


if __name__ == "__main__":
    main()
