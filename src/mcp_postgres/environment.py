"""Environment self-advertisement: what cluster and host mcp-postgres is bound to.

The capability report (``capabilities.py``) tells an agent what it is *allowed* to
do; this module tells it *what it is talking to* — the PostgreSQL version, the
extensions available / activated / preloaded, and the host OS. mcp-postgres runs on
the PostgreSQL host (it edits the local ``postgresql.conf`` and connects over
``127.0.0.1``), so the host OS is the cluster's OS.

Version and OS are fixed for the process lifetime (a change needs a restart), so
they are probed once and cached. Extension state is per-database and mutable
(``CREATE EXTENSION``, a config reload), so it is probed on every report.
"""

from __future__ import annotations

import logging
import platform

log = logging.getLogger(__name__)

_os_cache: dict | None = None
_version_cache: dict | None = None


def host_os() -> dict:
    """OS family/version of the host (== the PostgreSQL cluster host).

    Reads ``/etc/os-release`` via ``platform.freedesktop_os_release()`` (Python
    3.10+), falling back to ``platform.system()``/``release()`` when it is absent
    (a non-Linux dev host). Cached for the process lifetime.
    """
    global _os_cache
    if _os_cache is not None:
        return _os_cache
    info = {
        "family": platform.system() or None,  # 'Linux', 'Windows', 'Darwin'
        "name": None,
        "version": None,
        "pretty_name": None,
        "kernel": platform.release() or None,
        "arch": platform.machine() or None,
    }
    try:
        rel = platform.freedesktop_os_release()
        info["family"] = rel.get("ID") or info["family"]
        info["name"] = rel.get("NAME")
        info["version"] = rel.get("VERSION_ID") or rel.get("VERSION")
        info["pretty_name"] = rel.get("PRETTY_NAME")
    except OSError:
        # No os-release file (e.g. a Windows/macOS dev host); keep the fallback.
        pass
    _os_cache = info
    return info


def server_version(db) -> dict:
    """Full PostgreSQL version of the connected cluster. Cached process-wide."""
    global _version_cache
    if _version_cache is not None:
        return _version_cache
    row = db.query_one(
        "SELECT version() AS version_string, "
        "current_setting('server_version') AS server_version, "
        "current_setting('server_version_num')::int AS version_num"
    ) or {}
    num = row.get("version_num")
    info = {
        "version_string": row.get("version_string"),
        "server_version": row.get("server_version"),
        "version_num": num,
        "major": num // 10000 if isinstance(num, int) else None,
    }
    _version_cache = info
    return info


def extensions(db) -> dict:
    """The three-dimensional extension picture for the current database.

    * ``activated`` — created in this database (name + installed version).
    * ``available`` — present on disk but not created here (name + default version).
    * ``preloaded_libraries`` — ``shared_preload_libraries`` (loaded into the server).

    The three facets are parallel, not nested: a preload-only module has no SQL
    extension, and an extension can be activated without being preloaded.
    """
    _cols, rows = db.select(
        "SELECT name, default_version, installed_version "
        "FROM pg_available_extensions ORDER BY name"
    )
    activated, available = [], []
    for name, default_version, installed_version in rows:
        if installed_version is not None:
            activated.append({"name": name, "version": installed_version})
        else:
            available.append({"name": name, "default_version": default_version})
    raw = db.query_scalar("SELECT current_setting('shared_preload_libraries')") or ""
    preloaded = [lib.strip() for lib in raw.split(",") if lib.strip()]
    return {
        "activated": activated,
        "available": available,
        "preloaded_libraries": preloaded,
    }


def probe(db) -> dict:
    """Compose the full environment block, degrading each part independently.

    A probe failure is reported as ``{"error": ...}`` for that sub-block and never
    propagates — the capability report must always return the privilege tiers.
    """
    env: dict = {}
    try:
        env["os"] = host_os()
    except Exception as exc:  # noqa: BLE001 - never break the capability report
        log.warning("environment: OS probe failed: %s", exc)
        env["os"] = {"error": str(exc)}
    try:
        env["postgresql"] = _postgres(db)
    except Exception as exc:  # noqa: BLE001
        log.warning("environment: PostgreSQL probe failed: %s", exc)
        env["postgresql"] = {"error": str(exc)}
    return env


def _postgres(db) -> dict:
    info = dict(server_version(db))
    try:
        info["extensions"] = extensions(db)
    except Exception as exc:  # noqa: BLE001 - extensions degrade without losing version
        log.warning("environment: extensions probe failed: %s", exc)
        info["extensions"] = {"error": str(exc)}
    return info
