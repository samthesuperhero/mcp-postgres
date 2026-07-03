"""Registry of per-database connection targets.

Role ``mcp`` is cluster-global, so one set of credentials (host/port/user/password
from config) reaches every database in the local PostgreSQL cluster; only the
database *name* varies. Each distinct database therefore gets its own connection
pool and its own capability probe — the DB tier (e.g. ``CREATE`` on ``public``)
is measured per database, while the OS tier / privhelper is process-global and
shared.

A process-wide "current" target is what every tool acts on; the ``use_database``
tool switches it. Pools are created lazily and cached (``min_size=0`` keeps idle
pools cheap), so touching several databases in a session costs one pool each.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from .capabilities import CapabilityManager
from .config import DatabaseConfig
from .db import Database
from .privclient import PrivClient

log = logging.getLogger(__name__)


@dataclass
class Target:
    """A single database: its name, connection pool, and capability probe."""

    dbname: str
    db: Database
    caps: CapabilityManager


class DatabaseManager:
    def __init__(self, base: DatabaseConfig, priv: PrivClient):
        # ``base`` supplies host/port/user/password; its ``dbname`` is the default
        # (and initial current) database.
        self._base = base
        self._priv = priv
        self._targets: dict[str, Target] = {}
        self.default: str = base.dbname
        self.current: str = base.dbname

    def _make(self, dbname: str) -> Target:
        cfg = replace(self._base, dbname=dbname)
        db = Database(cfg)
        db.open()  # lazy: min_size=0 means no connection is opened until first use
        return Target(dbname=dbname, db=db, caps=CapabilityManager(db, self._priv))

    def get(self, dbname: str | None = None) -> Target:
        """Return the cached target for ``dbname`` (the current one if omitted),
        creating and caching it on first use."""
        dbname = dbname or self.current
        target = self._targets.get(dbname)
        if target is None:
            target = self._make(dbname)
            self._targets[dbname] = target
        return target

    def current_target(self) -> Target:
        return self.get(self.current)

    def use(self, dbname: str) -> Target:
        """Switch the current target to ``dbname`` after verifying it is reachable.

        On a connection/probe failure the just-created pool is discarded and the
        current target is left unchanged, so a bad name never strands the session.
        Raises ``ConnectionError`` with the underlying message on failure.
        """
        newly_created = dbname not in self._targets
        target = self.get(dbname)
        info = target.caps.db_info(force=True)  # forces the first real connection
        if info.get("error"):
            if newly_created:
                target.db.close()
                self._targets.pop(dbname, None)
            raise ConnectionError(info["error"])
        self.current = dbname
        return target

    def close(self) -> None:
        for target in self._targets.values():
            target.db.close()
        self._targets.clear()
