"""Config-file tools (postgresql.conf / pg_hba.conf). Require OS tier OS_CONFIG.

Every write goes through the privhelper, which independently enforces the
two-file allowlist and writes a timestamped backup. After a successful change the
service reloads PostgreSQL (unless the setting needs a full restart).
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

from ..capabilities import DbTier, OsTier
from ..confedit import append_hba_rule, set_conf_value
from .base import attach, guard_or_error

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# Config writes edit a file in place (with a timestamped backup) but are not
# "destructive" in the delete-data sense; re-applying the same change is a no-op.
_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


def _reload_postgres(ctx) -> tuple[bool, str | None]:
    """Try to reload PostgreSQL: privhelper first, then pg_reload_conf() if admin."""
    try:
        ctx.priv.reload()
        return True, None
    except Exception as exc:  # noqa: BLE001
        if ctx.caps.db_tier() >= DbTier.DB_ADMIN:
            try:
                ctx.db.query_scalar("SELECT pg_reload_conf()")
                return True, None
            except Exception as exc2:  # noqa: BLE001
                return False, str(exc2)
        return False, str(exc)


def register(mcp, ctx) -> None:
    caps = ctx.caps
    db = ctx.db
    priv = ctx.priv

    @mcp.tool(title="Read postgresql.conf", annotations=_READ_ONLY)
    def read_postgresql_conf() -> dict:
        """Read the contents of postgresql.conf. Requires OS_CONFIG."""
        allowed, info = guard_or_error(caps, os_min=OsTier.OS_CONFIG)
        if not allowed:
            return info
        try:
            content = priv.read("postgresql.conf")
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info)
        return attach({"ok": True, "file": "postgresql.conf", "content": content}, info)

    @mcp.tool(title="Read pg_hba.conf", annotations=_READ_ONLY)
    def read_pg_hba_conf() -> dict:
        """Read the contents of pg_hba.conf. Requires OS_CONFIG."""
        allowed, info = guard_or_error(caps, os_min=OsTier.OS_CONFIG)
        if not allowed:
            return info
        try:
            content = priv.read("pg_hba.conf")
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info)
        return attach({"ok": True, "file": "pg_hba.conf", "content": content}, info)

    @mcp.tool(title="Update postgresql.conf setting", annotations=_WRITE)
    def update_postgresql_setting(name: str, value: str, reload: str = "auto") -> dict:
        """Set a postgresql.conf parameter (with backup), then reload if applicable.

        ``reload`` is one of ``auto`` (reload on change unless a restart is
        required), ``true``, or ``false``. Requires OS_CONFIG.
        """
        allowed, info = guard_or_error(caps, os_min=OsTier.OS_CONFIG)
        if not allowed:
            return info
        try:
            content = priv.read("postgresql.conf")
            new_content, changed, old = set_conf_value(content, name, value)
            if changed:
                priv.write("postgresql.conf", new_content)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info)

        row = db.query_one("SELECT context FROM pg_settings WHERE name = %s", (name,))
        context = row["context"] if row else None
        restart_required = context == "postmaster"

        reloaded, reload_error = False, None
        want_reload = str(reload).lower() in ("auto", "true", "1", "yes")
        if changed and want_reload and not restart_required:
            reloaded, reload_error = _reload_postgres(ctx)

        result = {
            "ok": True,
            "file": "postgresql.conf",
            "setting": name,
            "changed": changed,
            "old_value": old,
            "new_value": value,
            "setting_context": context,
            "restart_required": restart_required,
            "reloaded": reloaded,
        }
        if reload_error:
            result["reload_error"] = reload_error
        if restart_required and changed:
            result["note"] = (
                "This setting requires a full PostgreSQL restart; the service does "
                "not restart PostgreSQL automatically."
            )
        return attach(result, info)

    @mcp.tool(title="Append pg_hba.conf rule", annotations=_WRITE)
    def update_pg_hba_rule(rule: str, reload: str = "auto") -> dict:
        """Append a pg_hba.conf rule line (with backup), then reload. Requires OS_CONFIG.

        Example rule: ``host mydb myuser 127.0.0.1/32 scram-sha-256``.
        """
        allowed, info = guard_or_error(caps, os_min=OsTier.OS_CONFIG)
        if not allowed:
            return info
        try:
            content = priv.read("pg_hba.conf")
            new_content, changed = append_hba_rule(content, rule)
            if changed:
                priv.write("pg_hba.conf", new_content)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info)

        reloaded, reload_error = False, None
        want_reload = str(reload).lower() in ("auto", "true", "1", "yes")
        if changed and want_reload:
            reloaded, reload_error = _reload_postgres(ctx)

        result = {
            "ok": True,
            "file": "pg_hba.conf",
            "rule": rule,
            "changed": changed,
            "reloaded": reloaded,
        }
        if reload_error:
            result["reload_error"] = reload_error
        return attach(result, info)

    @mcp.tool(title="Reload PostgreSQL config", annotations=_WRITE)
    def reload_postgresql() -> dict:
        """Reload PostgreSQL configuration. Requires OS_CONFIG or DB_ADMIN."""
        # Allowed if we can reload via sudo OR via SQL as an admin role.
        if caps.os_tier() < OsTier.OS_CONFIG and caps.db_tier() < DbTier.DB_ADMIN:
            allowed, info = guard_or_error(caps, os_min=OsTier.OS_CONFIG)
            return info  # will be the refusal dict
        notices = caps.guard()
        reloaded, reload_error = _reload_postgres(ctx)
        result = {"ok": reloaded, "reloaded": reloaded}
        if reload_error:
            result["error"] = reload_error
        return attach(result, notices)

    if caps.os_tier() >= OsTier.OS_CONFIG:
        ctx.enabled_tools += [
            "read_postgresql_conf",
            "read_pg_hba_conf",
            "update_postgresql_setting",
            "update_pg_hba_rule",
        ]
    if caps.os_tier() >= OsTier.OS_CONFIG or caps.db_tier() >= DbTier.DB_ADMIN:
        ctx.enabled_tools.append("reload_postgresql")
