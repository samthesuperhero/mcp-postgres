"""Config-file tools (postgresql.conf / pg_hba.conf). Require OS tier OS_CONFIG.

Every write goes through the privhelper, which independently enforces the
two-file allowlist and writes a timestamped backup. After a successful change the
service reloads PostgreSQL (unless the setting needs a full restart).
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

from ..capabilities import DbTier, OsTier
from ..confedit import append_hba_rule, is_shadowed_source, set_conf_value
from .base import attach, guard_or_error

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def _resolve_path(target, key: str) -> str | None:
    """Absolute config_file / hba_file path as PostgreSQL reports it (or None).

    The capability probe (``current_setting('config_file'|'hba_file')``) already
    discovered these into ``db_info()``; the guard that precedes every config tool
    populates that cache, so this is a cheap dict lookup with no extra round-trip.
    The privhelper needs the full path — a bare basename would resolve against its
    CWD (``/`` under sudo) and be rejected.
    """
    return target.caps.db_info().get(key)
# Config writes edit a file in place (with a timestamped backup) but are not
# "destructive" in the delete-data sense; re-applying the same change is a no-op.
_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


def _reload_postgres(ctx, target) -> tuple[bool, str | None]:
    """Try to reload PostgreSQL: privhelper first, then pg_reload_conf() if admin.

    The SQL fallback runs against ``target`` (the current database); config is
    cluster-global, so any connection with DB_ADMIN can issue the reload.
    """
    try:
        ctx.priv.reload()
        return True, None
    except Exception as exc:  # noqa: BLE001
        if target.caps.db_tier() >= DbTier.DB_ADMIN:
            try:
                target.db.query_scalar("SELECT pg_reload_conf()")
                return True, None
            except Exception as exc2:  # noqa: BLE001
                return False, str(exc2)
        return False, str(exc)


def register(mcp, ctx) -> None:
    priv = ctx.priv

    @mcp.tool(title="Read postgresql.conf", annotations=_READ_ONLY)
    def read_postgresql_conf() -> dict:
        """Read the contents of postgresql.conf. Requires OS_CONFIG."""
        target = ctx.manager.current_target()
        allowed, info = guard_or_error(target.caps, os_min=OsTier.OS_CONFIG, database=target.dbname)
        if not allowed:
            return info
        path = _resolve_path(target, "config_file")
        if not path:
            return attach(
                {"ok": False, "error": "could not determine the postgresql.conf path "
                                       "(config_file is unset; the database may be unreachable)"},
                info, database=target.dbname,
            )
        try:
            content = priv.read(path)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info, database=target.dbname)
        return attach(
            {"ok": True, "file": "postgresql.conf", "path": path, "content": content},
            info, database=target.dbname,
        )

    @mcp.tool(title="Read pg_hba.conf", annotations=_READ_ONLY)
    def read_pg_hba_conf() -> dict:
        """Read the contents of pg_hba.conf. Requires OS_CONFIG."""
        target = ctx.manager.current_target()
        allowed, info = guard_or_error(target.caps, os_min=OsTier.OS_CONFIG, database=target.dbname)
        if not allowed:
            return info
        path = _resolve_path(target, "hba_file")
        if not path:
            return attach(
                {"ok": False, "error": "could not determine the pg_hba.conf path "
                                       "(hba_file is unset; the database may be unreachable)"},
                info, database=target.dbname,
            )
        try:
            content = priv.read(path)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info, database=target.dbname)
        return attach(
            {"ok": True, "file": "pg_hba.conf", "path": path, "content": content},
            info, database=target.dbname,
        )

    @mcp.tool(title="Update postgresql.conf setting", annotations=_WRITE)
    def update_postgresql_setting(name: str, value: str, reload: str = "auto") -> dict:
        """Set a postgresql.conf parameter (with backup), then reload if applicable.

        ``reload`` is one of ``auto`` (reload on change unless a restart is
        required), ``true``, or ``false``. Requires OS_CONFIG.
        """
        target = ctx.manager.current_target()
        allowed, info = guard_or_error(target.caps, os_min=OsTier.OS_CONFIG, database=target.dbname)
        if not allowed:
            return info
        path = _resolve_path(target, "config_file")
        if not path:
            return attach(
                {"ok": False, "error": "could not determine the postgresql.conf path "
                                       "(config_file is unset; the database may be unreachable)"},
                info, database=target.dbname,
            )
        try:
            content = priv.read(path)
            edit = set_conf_value(content, name, value)
            if edit.changed:
                priv.write(path, edit.content)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info, database=target.dbname)

        # pg_settings still reflects the config loaded before this write, so
        # `sourcefile` names the file currently supplying the effective value — used
        # below to warn when another file (e.g. postgresql.auto.conf) shadows our edit.
        row = target.db.query_one(
            "SELECT context, sourcefile FROM pg_settings WHERE name = %s", (name,)
        )
        context = row["context"] if row else None
        sourcefile = row.get("sourcefile") if row else None
        restart_required = context == "postmaster"

        reloaded, reload_error = False, None
        want_reload = str(reload).lower() in ("auto", "true", "1", "yes")
        if edit.changed and want_reload and not restart_required:
            reloaded, reload_error = _reload_postgres(ctx, target)

        result = {
            "ok": True,
            "file": "postgresql.conf",
            "path": path,
            "setting": name,
            "changed": edit.changed,
            "action": edit.action,
            "active_occurrences": edit.active_occurrences,
            "old_value": edit.old_value,
            "new_value": value,
            "setting_context": context,
            "restart_required": restart_required,
            "reloaded": reloaded,
        }
        if reload_error:
            result["reload_error"] = reload_error

        notes: list[str] = []
        if edit.shadowed_disabled:
            result["duplicates_disabled"] = edit.shadowed_disabled
            notes.append(
                f"Found {edit.active_occurrences} active '{name}' lines; PostgreSQL uses the "
                f"last one, so the effective line was updated and {edit.shadowed_disabled} "
                f"earlier duplicate(s) were commented out to leave a single unambiguous setting."
            )
        if is_shadowed_source(path, sourcefile):
            result["effective_source_file"] = sourcefile
            notes.append(
                f"'{name}' is currently supplied by {sourcefile}, not the file just edited. That "
                f"file overrides postgresql.conf (e.g. postgresql.auto.conf written by ALTER "
                f"SYSTEM, or a later include), so this change will not take effect until '{name}' "
                f"is updated or removed there."
            )
        if restart_required and edit.changed:
            notes.append(
                "This setting requires a full PostgreSQL restart; the service does "
                "not restart PostgreSQL automatically."
            )
        if notes:
            result["note"] = " ".join(notes)
        return attach(result, info, database=target.dbname)

    @mcp.tool(title="Append pg_hba.conf rule", annotations=_WRITE)
    def update_pg_hba_rule(rule: str, reload: str = "auto") -> dict:
        """Append a pg_hba.conf rule line (with backup), then reload. Requires OS_CONFIG.

        Example rule: ``host mydb myuser 127.0.0.1/32 scram-sha-256``.
        """
        target = ctx.manager.current_target()
        allowed, info = guard_or_error(target.caps, os_min=OsTier.OS_CONFIG, database=target.dbname)
        if not allowed:
            return info
        path = _resolve_path(target, "hba_file")
        if not path:
            return attach(
                {"ok": False, "error": "could not determine the pg_hba.conf path "
                                       "(hba_file is unset; the database may be unreachable)"},
                info, database=target.dbname,
            )
        try:
            content = priv.read(path)
            new_content, changed = append_hba_rule(content, rule)
            if changed:
                priv.write(path, new_content)
        except Exception as exc:  # noqa: BLE001
            return attach({"ok": False, "error": str(exc)}, info, database=target.dbname)

        reloaded, reload_error = False, None
        want_reload = str(reload).lower() in ("auto", "true", "1", "yes")
        if changed and want_reload:
            reloaded, reload_error = _reload_postgres(ctx, target)

        result = {
            "ok": True,
            "file": "pg_hba.conf",
            "path": path,
            "rule": rule,
            "changed": changed,
            "reloaded": reloaded,
        }
        if reload_error:
            result["reload_error"] = reload_error
        return attach(result, info, database=target.dbname)

    @mcp.tool(title="Reload PostgreSQL config", annotations=_WRITE)
    def reload_postgresql() -> dict:
        """Reload PostgreSQL configuration. Requires OS_CONFIG or DB_ADMIN."""
        target = ctx.manager.current_target()
        caps = target.caps
        # Allowed if we can reload via sudo OR via SQL as an admin role.
        if caps.os_tier() < OsTier.OS_CONFIG and caps.db_tier() < DbTier.DB_ADMIN:
            _allowed, info = guard_or_error(caps, os_min=OsTier.OS_CONFIG, database=target.dbname)
            return info  # will be the refusal dict
        notices = caps.guard()
        reloaded, reload_error = _reload_postgres(ctx, target)
        result = {"ok": reloaded, "reloaded": reloaded}
        if reload_error:
            result["error"] = reload_error
        return attach(result, notices, database=target.dbname)
