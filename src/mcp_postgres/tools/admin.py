"""Database administration tools.

``grant``, ``revoke`` and ``admin_sql`` require DB tier ``DB_ADMIN`` (i.e. the role is
a superuser). ``create_database`` and ``create_role`` require only the matching role
attribute (``CREATEDB`` / ``CREATEROLE``) — an independent capability — so they work
without full admin.

Tools are always registered so a role that gains rights mid-session is usable without a
restart; the guard enforces the requirement on every call, and PostgreSQL itself remains
the final authority on every statement.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations
from psycopg import sql

from ..capabilities import DbTier
from .base import attach, guard_or_error


def register(mcp, ctx) -> None:
    @mcp.tool(
        title="Create database",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False
        ),
    )
    def create_database(name: str, owner: str | None = None) -> dict:
        """Create a database, optionally owned by a role. Requires CREATEDB (or superuser)."""
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_needs=("createdb",), database=t.dbname)
        if not allowed:
            return info
        stmt = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
        if owner:
            stmt = stmt + sql.SQL(" OWNER {}").format(sql.Identifier(owner))
        try:
            t.db.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info, database=t.dbname)
        return attach({"ok": True, "created_database": name, "owner": owner}, info, database=t.dbname)

    @mcp.tool(
        title="Create role",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False
        ),
    )
    def create_role(
        name: str,
        login: bool = True,
        password: str | None = None,
        createdb: bool = False,
        createrole: bool = False,
    ) -> dict:
        """Create a role. Requires CREATEROLE (or superuser)."""
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_needs=("createrole",), database=t.dbname)
        if not allowed:
            return info
        opts = [sql.SQL("LOGIN" if login else "NOLOGIN")]
        if createdb:
            opts.append(sql.SQL("CREATEDB"))
        if createrole:
            opts.append(sql.SQL("CREATEROLE"))
        stmt = sql.SQL("CREATE ROLE {} WITH {}").format(
            sql.Identifier(name), sql.SQL(" ").join(opts)
        )
        if password:
            stmt = stmt + sql.SQL(" PASSWORD {}").format(sql.Literal(password))
        try:
            t.db.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info, database=t.dbname)
        return attach({"ok": True, "role": name}, info, database=t.dbname)

    @mcp.tool(
        title="Grant privileges",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    def grant(privileges: str, on_object: str, to_role: str) -> dict:
        """Run GRANT <privileges> ON <on_object> TO <role>. Requires DB_ADMIN."""
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_ADMIN, database=t.dbname)
        if not allowed:
            return info
        try:
            t.db.execute(f"GRANT {privileges} ON {on_object} TO {to_role}")
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info, database=t.dbname)
        return attach(
            {"ok": True, "granted": privileges, "on": on_object, "to": to_role},
            info,
            database=t.dbname,
        )

    @mcp.tool(
        title="Revoke privileges",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
        ),
    )
    def revoke(privileges: str, on_object: str, from_role: str) -> dict:
        """Run REVOKE <privileges> ON <on_object> FROM <role>. Requires DB_ADMIN."""
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_ADMIN, database=t.dbname)
        if not allowed:
            return info
        try:
            t.db.execute(f"REVOKE {privileges} ON {on_object} FROM {from_role}")
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info, database=t.dbname)
        return attach(
            {"ok": True, "revoked": privileges, "on": on_object, "from": from_role},
            info,
            database=t.dbname,
        )

    @mcp.tool(
        title="Administrative SQL",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, openWorldHint=False
        ),
    )
    def admin_sql(sql_text: str) -> dict:
        """Execute an arbitrary administrative statement. Requires DB_ADMIN.

        Do not use this to bypass a dedicated tool — e.g. ALTER SYSTEM for configuration
        that update_postgresql_setting handles. Warn the user before any such workaround.
        """
        t = ctx.manager.current_target()
        allowed, info = guard_or_error(t.caps, db_min=DbTier.DB_ADMIN, database=t.dbname)
        if not allowed:
            return info
        try:
            result = t.db.execute(sql_text)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info, database=t.dbname)
        result["ok"] = True
        return attach(result, info, database=t.dbname)
