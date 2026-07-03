#!/usr/bin/env python3
"""Toggle mcp-postgres admin rights (OS wheel + DB superuser), in lockstep.

Run once, as root, from the repo directory (same as install.py):

    sudo python3 mcp-postgres/admin.py            # toggle both sides
    sudo python3 mcp-postgres/admin.py --status   # report only, no change

`mcp-postgres` is privilege self-aware: it measures two independent tiers and
re-checks them before every action (see ARCHITECTURE.md sec.4). This tool flips
both tiers together so an operator can grant or revoke admin, or exercise the
runtime re-check, in one step.

Two dimensions, kept aligned:

* OS  — can the service user run the privhelper via passwordless sudo (=> it may
        read/modify postgresql.conf/pg_hba.conf and reload)? Lever: wheel
        membership + the scoped /etc/sudoers.d/mcp-postgres NOPASSWD drop-in.
        Probed exactly as the service does:
            sudo -u mcp-postgres sudo -n <privhelper> --check
* DB  — is role `mcp` a PostgreSQL superuser? Lever: ALTER ROLE ... SUPERUSER,
        run via `sudo -u postgres psql` (peer auth), detected via pg_roles.rolsuper.

If the two sides already agree, they are toggled to the opposite state. If they
disagree (e.g. changed out of band, or a prior run half-failed), both are driven
to OFF. Stdlib only -- it must run before/without the application venv.
"""

from __future__ import annotations

import argparse
import grp
import os
import pwd
import re
import shutil
import subprocess
import sys
import tomllib

# Reuse the installer's constants and its visudo-validated sudoers installer.
import install

PRIVHELPER = install.LIBEXEC_DIR / "privhelper"
ROLE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def info(msg: str) -> None:
    print(f"[admin] {msg}")


def warn(msg: str) -> None:
    print(f"[admin] WARNING: {msg}")


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"[admin] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd: list[str], *, check: bool = True, cwd: str | None = None) -> subprocess.CompletedProcess:
    info("run: " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if check and proc.returncode != 0:
        err = (proc.stderr or "").strip()
        die(f"command failed ({proc.returncode}): {' '.join(cmd)}" + (f"\n{err}" if err else ""))
    return proc


# -- helpers -----------------------------------------------------------------


def _onoff(b: bool) -> str:
    return "ADMIN" if b else "not-admin"


def _yn(b: bool) -> str:
    return "yes" if b else "no"


def in_wheel() -> bool:
    try:
        return install.SERVICE_USER in grp.getgrnam("wheel").gr_mem
    except KeyError:
        return False


# -- preflight ---------------------------------------------------------------


def preflight() -> None:
    if os.geteuid() != 0:
        die("must run as root (use sudo)")
    if sys.version_info < (3, 11):
        die(f"Python 3.11+ required, have {sys.version.split()[0]}")
    try:
        pwd.getpwnam(install.SERVICE_USER)
    except KeyError:
        die(f"service user {install.SERVICE_USER!r} not found -- run install.py first")
    try:
        pwd.getpwnam("postgres")
    except KeyError:
        die("OS user 'postgres' not found -- cannot manage the DB role")
    try:
        grp.getgrnam("wheel")
    except KeyError:
        die("'wheel' group not present on this host")
    missing = [b for b in ("sudo", "usermod", "gpasswd", "visudo", "psql") if not shutil.which(b)]
    if missing:
        die("missing required commands: " + ", ".join(missing))
    if not PRIVHELPER.exists():
        die(f"privhelper not found at {PRIVHELPER} -- run install.py first")


# -- role resolution ---------------------------------------------------------


def resolve_role(override: str | None) -> str:
    role = override
    if role is None:
        cfg = install.CONFIG_DIR / "config.toml"
        role = "mcp"
        if cfg.exists():
            with cfg.open("rb") as fh:
                data = tomllib.load(fh)
            role = str(data.get("database", {}).get("user") or "mcp")
    if not ROLE_RE.fullmatch(role):
        die(f"unsafe DB role name: {role!r}")
    return role


# -- probes ------------------------------------------------------------------


def probe_os() -> dict:
    """Ground-truth OS-admin probe: exactly what the service runs at runtime."""
    proc = run(
        ["sudo", "-u", install.SERVICE_USER, "sudo", "-n", str(PRIVHELPER), "--check"],
        check=False,
    )
    return {
        "admin": proc.returncode == 0,
        "wheel_member": in_wheel(),
        "sudoers_dropin": install.SUDOERS_PATH.exists(),
    }


def probe_db(role: str) -> dict:
    sql = (
        "SELECT rolsuper, rolcreatedb, rolcreaterole "
        f"FROM pg_roles WHERE rolname = '{role}'"
    )
    proc = run(
        ["sudo", "-u", "postgres", "psql", "-tAqc", sql],
        cwd="/",  # avoid "could not change directory" noise from postgres' cwd
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.strip()), "")
    if not line:
        die(f"PostgreSQL role {role!r} not found -- create it (install.py --create-db-role)")
    parts = line.split("|")
    if len(parts) != 3:
        die(f"unexpected psql output for role {role!r}: {line!r}")
    return {
        "rolsuper": parts[0] == "t",
        "rolcreatedb": parts[1] == "t",
        "rolcreaterole": parts[2] == "t",
    }


# -- apply -------------------------------------------------------------------


def set_wheel(member: bool) -> None:
    currently = in_wheel()
    if member and not currently:
        run(["usermod", "-aG", "wheel", install.SERVICE_USER])
        info(f"added {install.SERVICE_USER} to wheel")
    elif not member and currently:
        run(["gpasswd", "-d", install.SERVICE_USER, "wheel"])
        info(f"removed {install.SERVICE_USER} from wheel")
    else:
        info(f"wheel membership already {'present' if member else 'absent'}")


def apply_os(target: bool) -> None:
    if target:
        set_wheel(True)
        install.install_sudoers()  # visudo-validated; installs the NOPASSWD drop-in
    else:
        set_wheel(False)
        if install.SUDOERS_PATH.exists():
            install.SUDOERS_PATH.unlink()
            info(f"removed {install.SUDOERS_PATH}")
        else:
            info(f"{install.SUDOERS_PATH} already absent")


def apply_db(target: bool, role: str) -> None:
    attr = "SUPERUSER" if target else "NOSUPERUSER"
    run(
        ["sudo", "-u", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-c", f"ALTER ROLE {role} {attr};"],
        cwd="/",
    )
    info(f"ALTER ROLE {role} {attr}")


# -- reporting ---------------------------------------------------------------


def print_state(label: str, os_info: dict, db: dict) -> None:
    info(f"{label}:")
    info(
        f"    OS  {_onoff(os_info['admin'])}"
        f"  (wheel_member={_yn(os_info['wheel_member'])},"
        f" sudoers_dropin={_yn(os_info['sudoers_dropin'])},"
        f" privhelper_check={'pass' if os_info['admin'] else 'fail'})"
    )
    info(
        f"    DB  {_onoff(db['rolsuper'])}"
        f"  (superuser={_yn(db['rolsuper'])},"
        f" createdb={_yn(db['rolcreatedb'])},"
        f" createrole={_yn(db['rolcreaterole'])})"
    )
    if not db["rolsuper"] and (db["rolcreatedb"] or db["rolcreaterole"]):
        warn(
            "role has createdb/createrole set without superuser -- the service "
            "still reports DB_ADMIN (see capabilities.py); this tool only toggles SUPERUSER"
        )


# -- main --------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description="Toggle mcp-postgres admin rights (OS wheel + DB superuser)")
    p.add_argument("--status", action="store_true", help="report current state and exit without changing anything")
    p.add_argument("--db-user", help="PostgreSQL role to toggle (default: from config.toml, else 'mcp')")
    args = p.parse_args()

    preflight()
    role = resolve_role(args.db_user)

    os_info = probe_os()
    db = probe_db(role)
    print_state("current state", os_info, db)

    if args.status:
        return

    os_admin = os_info["admin"]
    db_admin = db["rolsuper"]

    if os_admin == db_admin:
        target = not os_admin
        info(f"sides aligned ({_onoff(os_admin)}) -- toggling to {_onoff(target)}")
    else:
        target = False
        warn(f"sides disagree (OS={_onoff(os_admin)}, DB={_onoff(db_admin)}) -- reconciling both to not-admin")

    apply_os(target)
    apply_db(target, role)

    os_info2 = probe_os()
    db2 = probe_db(role)
    print_state("new state", os_info2, db2)

    ok = (os_info2["admin"] == target) and (db2["rolsuper"] == target)
    verdict = "ENABLED" if target else "DISABLED"
    if ok:
        info(f"done: admin rights {verdict}")
        sys.exit(0)
    else:
        die(
            f"admin rights {verdict} -- FAILED: "
            f"OS reached {_onoff(os_info2['admin'])}, DB reached {_onoff(db2['rolsuper'])} "
            f"(wanted {_onoff(target)})"
        )


if __name__ == "__main__":
    main()
