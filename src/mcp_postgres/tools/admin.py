"""Database administration tools. Require DB tier DB_ADMIN.

Tools are always registered so a role that gains admin rights mid-session is
usable without a restart; the guard enforces the tier on every call, and PostgreSQL
itself remains the final authority on every statement.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations
from psycopg import sql

from ..capabilities import DbTier
from .base import attach, guard_or_error


def register(mcp, ctx) -> None:
    caps = ctx.caps
    db = ctx.db

    @mcp.tool(
        title="Create database",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False
        ),
    )
    def create_database(name: str, owner: str | None = None) -> dict:
        """Create a database, optionally owned by a role. Requires DB_ADMIN."""
        allowed, info = guard_or_error(caps, db_min=DbTier.DB_ADMIN)
        if not allowed:
            return info
        stmt = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
        if owner:
            stmt = stmt + sql.SQL(" OWNER {}").format(sql.Identifier(owner))
        try:
            db.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info)
        return attach({"ok": True, "database": name, "owner": owner}, info)

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
        """Create a role. Requires DB_ADMIN."""
        allowed, info = guard_or_error(caps, db_min=DbTier.DB_ADMIN)
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
            db.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info)
        return attach({"ok": True, "role": name}, info)

    @mcp.tool(
        title="Grant privileges",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    def grant(privileges: str, on_object: str, to_role: str) -> dict:
        """Run GRANT <privileges> ON <on_object> TO <role>. Requires DB_ADMIN."""
        allowed, info = guard_or_error(caps, db_min=DbTier.DB_ADMIN)
        if not allowed:
            return info
        try:
            db.execute(f"GRANT {privileges} ON {on_object} TO {to_role}")
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info)
        return attach({"ok": True, "granted": privileges, "on": on_object, "to": to_role}, info)

    @mcp.tool(
        title="Revoke privileges",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
        ),
    )
    def revoke(privileges: str, on_object: str, from_role: str) -> dict:
        """Run REVOKE <privileges> ON <on_object> FROM <role>. Requires DB_ADMIN."""
        allowed, info = guard_or_error(caps, db_min=DbTier.DB_ADMIN)
        if not allowed:
            return info
        try:
            db.execute(f"REVOKE {privileges} ON {on_object} FROM {from_role}")
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info)
        return attach({"ok": True, "revoked": privileges, "on": on_object, "from": from_role}, info)

    @mcp.tool(
        title="Administrative SQL",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, openWorldHint=False
        ),
    )
    def admin_sql(sql_text: str) -> dict:
        """Execute an arbitrary administrative statement. Requires DB_ADMIN."""
        allowed, info = guard_or_error(caps, db_min=DbTier.DB_ADMIN)
        if not allowed:
            return info
        try:
            result = db.execute(sql_text)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info)
        result["ok"] = True
        return attach(result, info)

    if caps.db_tier() >= DbTier.DB_ADMIN:
        ctx.enabled_tools += ["create_database", "create_role", "grant", "revoke", "admin_sql"]
