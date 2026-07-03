"""Capability self-check engine.

Probes two independent privilege dimensions and lets tools gate on them:

* **OS tier**  — can ``mcp-postgres`` run the privhelper via sudo?
    ``OS_NONE`` < ``OS_CONFIG``
* **DB tier**  — what can role ``mcp`` do inside PostgreSQL?
    ``DB_READONLY`` < ``DB_READWRITE`` < ``DB_ADMIN``

Probes are cached for a few seconds so a burst of calls isn't hammered, but the
``guard`` re-validates before every action and emits a human-readable notice
whenever a tier has changed since it was last observed. PostgreSQL remains the
real authority on DB permissions — the DB tier only decides which tools to
*offer*; an actual write attempt still fails if the role lacks the grant.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from enum import IntEnum

log = logging.getLogger(__name__)


class OsTier(IntEnum):
    OS_NONE = 0
    OS_CONFIG = 1


class DbTier(IntEnum):
    DB_READONLY = 0
    DB_READWRITE = 1
    DB_ADMIN = 2


class CapabilityError(Exception):
    """Raised by ``guard`` when the current tier is insufficient for an action."""

    def __init__(self, message: str, notices: list[str] | None = None):
        super().__init__(message)
        self.notices = notices or []


class CapabilityManager:
    def __init__(self, db, priv, cache_ttl: float = 5.0):
        self.db = db
        self.priv = priv
        self.cache_ttl = cache_ttl
        self._os_cache: tuple[OsTier, float] | None = None
        self._db_cache: tuple[dict, float] | None = None
        self._last_os: OsTier | None = None
        self._last_db: DbTier | None = None

    # -- OS tier --------------------------------------------------------------

    def os_tier(self, force: bool = False) -> OsTier:
        now = time.monotonic()
        if not force and self._os_cache and now - self._os_cache[1] < self.cache_ttl:
            return self._os_cache[0]
        tier = OsTier.OS_CONFIG if self.priv.check() else OsTier.OS_NONE
        self._os_cache = (tier, now)
        return tier

    # -- DB tier --------------------------------------------------------------

    def db_info(self, force: bool = False) -> dict:
        now = time.monotonic()
        if not force and self._db_cache and now - self._db_cache[1] < self.cache_ttl:
            return self._db_cache[0]
        info = self._probe_db()
        self._db_cache = (info, now)
        return info

    def db_tier(self, force: bool = False) -> DbTier:
        return self.db_info(force)["tier"]

    def _probe_db(self) -> dict:
        info: dict = {
            "tier": DbTier.DB_READONLY,
            "role": None,
            "attributes": {},
            "config_file": None,
            "hba_file": None,
            "error": None,
        }
        try:
            row = self.db.query_one(
                "SELECT current_user AS role, rolsuper, rolcreatedb, rolcreaterole "
                "FROM pg_roles WHERE rolname = current_user"
            ) or {}
            info["role"] = row.get("role")
            attrs = {
                "superuser": bool(row.get("rolsuper")),
                "createdb": bool(row.get("rolcreatedb")),
                "createrole": bool(row.get("rolcreaterole")),
            }
            info["attributes"] = attrs

            can_write = bool(
                self.db.query_scalar("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
            )

            if attrs["superuser"] or attrs["createdb"] or attrs["createrole"]:
                info["tier"] = DbTier.DB_ADMIN
            elif can_write:
                info["tier"] = DbTier.DB_READWRITE
            else:
                info["tier"] = DbTier.DB_READONLY

            # These are visible to any role; used to locate the editable files.
            info["config_file"] = self.db.query_scalar("SELECT current_setting('config_file', true)")
            info["hba_file"] = self.db.query_scalar("SELECT current_setting('hba_file', true)")
        except Exception as exc:  # noqa: BLE001 - DB may be down; degrade gracefully
            info["error"] = str(exc)
            log.warning("DB capability probe failed: %s", exc)
        return info

    # -- change detection + guard --------------------------------------------

    def _detect_changes(self) -> list[str]:
        notices: list[str] = []
        os_t = self.os_tier()
        if self._last_os is not None and os_t != self._last_os:
            notices.append(f"OS tier changed {self._last_os.name} -> {os_t.name}")
        self._last_os = os_t

        db_t = self.db_tier()
        if self._last_db is not None and db_t != self._last_db:
            notices.append(f"DB tier changed {self._last_db.name} -> {db_t.name}")
        self._last_db = db_t
        return notices

    def guard(self, os_min: OsTier | None = None, db_min: DbTier | None = None) -> list[str]:
        """Re-probe before an action. Returns change notices, or raises CapabilityError."""
        notices = self._detect_changes()
        if os_min is not None:
            cur = self.os_tier()
            if cur < os_min:
                raise CapabilityError(
                    f"requires OS tier {os_min.name}, current is {cur.name} "
                    f"(grant the mcp-postgres user sudo access to the privhelper)",
                    notices,
                )
        if db_min is not None:
            cur = self.db_tier()
            if cur < db_min:
                raise CapabilityError(
                    f"requires DB tier {db_min.name}, current is {cur.name} "
                    f"(the 'mcp' role lacks the required PostgreSQL privileges)",
                    notices,
                )
        return notices

    # -- report ---------------------------------------------------------------

    def report(self, enabled_tools: list[str]) -> dict:
        os_t = self.os_tier(force=True)
        info = self.db_info(force=True)
        return {
            "service": "mcp-postgres",
            "os_tier": os_t.name,
            "db_tier": info["tier"].name,
            "connected_role": info["role"],
            "role_attributes": info["attributes"],
            "config_file": info["config_file"],
            "hba_file": info["hba_file"],
            "database_error": info["error"],
            "enabled_tools": sorted(enabled_tools),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
