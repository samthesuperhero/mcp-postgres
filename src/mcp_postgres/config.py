"""Configuration loading for mcp-postgres.

Reads ``/etc/mcp-postgres/config.toml`` plus the ``secret`` (DB password) and
``token`` (MCP bearer token) files. The config directory can be overridden with
the ``MCP_PG_CONFIG_DIR`` environment variable (used by tests). Secrets may also
be supplied via ``MCP_PG_DB_PASSWORD`` / ``MCP_PG_TOKEN`` when no file exists.

This module is also the single source of truth for the config *schema*: the known
sections/keys (derived from the dataclasses below), the keys that must never live
in ``config.toml`` (secrets), and the registry of deprecated keys. ``diff_config``
compares a config file against that schema; ``configmigrate`` uses the same schema
to migrate a deployed file in place.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG_DIR = Path(os.environ.get("MCP_PG_CONFIG_DIR", "/etc/mcp-postgres"))


def _only_known(cls, data: dict) -> dict:
    """Drop unexpected keys so a stray config entry can't crash startup."""
    known = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in known}


@dataclass
class ServerConfig:
    bind: str = "127.0.0.1"
    port: int = 41780
    path: str = "/mcp"


@dataclass
class DatabaseConfig:
    host: str = "127.0.0.1"
    port: int = 5432
    user: str = "mcp"
    dbname: str = "postgres"
    password: str = ""


@dataclass
class LoggingConfig:
    level: str = "INFO"


@dataclass
class Config:
    server: ServerConfig
    database: DatabaseConfig
    log_level: str = "INFO"
    token: str = ""
    config_dir: Path = DEFAULT_CONFIG_DIR


# -- config schema (single source of truth) ----------------------------------

# Maps each ``[section]`` in config.toml to the dataclass that defines its keys
# and their defaults. Adding a field to one of these dataclasses automatically
# makes it a "known" key (and, on the next update, an operator advisory).
SECTION_MODELS: dict[str, type] = {
    "server": ServerConfig,
    "database": DatabaseConfig,
    "logging": LoggingConfig,
}

# Keys that are dataclass fields but must NEVER appear in config.toml because
# they are secrets sourced from a separate 0600 file (or an env var). These are
# excluded from every file-facing view of the schema (report + migration).
SECRET_KEYS: set[tuple[str, str]] = {("database", "password")}

# Registry of removed/renamed keys, ``(section, key) -> human reason``. Seeded
# empty. When a field is removed from a dataclass above, add its old name here in
# the same commit so the updater can flag (and comment out) a stale entry instead
# of silently ignoring it. Example:
#     ("server", "tls"): "removed in 0.3.0; nginx terminates TLS",
DEPRECATED_KEYS: dict[tuple[str, str], str] = {}


def file_known_keys() -> dict[str, dict[str, object]]:
    """Return ``section -> {key: default}`` for every key that may live in the
    config file (i.e. the dataclass fields, minus the secret keys)."""
    out: dict[str, dict[str, object]] = {}
    for section, model in SECTION_MODELS.items():
        out[section] = {
            f.name: f.default
            for f in fields(model)
            if (section, f.name) not in SECRET_KEYS
        }
    return out


# -- drift detection ----------------------------------------------------------


@dataclass
class ConfigReport:
    """The difference between a config.toml and the current schema.

    ``missing`` lists known keys the file neither sets nor mentions in a comment
    (the service silently uses their defaults). ``deprecated_present`` lists
    active keys the schema has retired. ``unknown_keys`` / ``unknown_sections``
    list active keys/sections that are neither known nor deprecated (typos, or
    keys removed without a registry entry).
    """

    missing: list[tuple[str, str, object]] = field(default_factory=list)
    deprecated_present: list[tuple[str, str, str]] = field(default_factory=list)
    unknown_keys: list[tuple[str, str]] = field(default_factory=list)
    unknown_sections: list[str] = field(default_factory=list)

    def any(self) -> bool:
        return bool(
            self.missing
            or self.deprecated_present
            or self.unknown_keys
            or self.unknown_sections
        )

    def summary(self) -> str:
        parts = []
        if self.missing:
            parts.append(f"{len(self.missing)} new")
        if self.deprecated_present:
            parts.append(f"{len(self.deprecated_present)} deprecated")
        if self.unknown_keys:
            parts.append(f"{len(self.unknown_keys)} unrecognized")
        if self.unknown_sections:
            parts.append(f"{len(self.unknown_sections)} unknown-section")
        return ", ".join(parts) if parts else "no drift"

    def messages(self) -> list[str]:
        """One human-readable advisory line per drift item."""
        out = []
        for section, key, default in self.missing:
            out.append(f"[{section}] {key} not set — using default {default!r}")
        for section, key, reason in self.deprecated_present:
            out.append(f"[{section}] {key} is deprecated (ignored): {reason}")
        for section, key in self.unknown_keys:
            out.append(f"[{section}] {key} is not a recognized setting (ignored)")
        for section in self.unknown_sections:
            out.append(f"[{section}] is not a recognized section (ignored)")
        return out


_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_COMMENTED_KEY_RE = re.compile(r"^\s*#\s*([A-Za-z_][\w-]*)\s*=")


def commented_keys(text: str) -> dict[str, set[str]]:
    """Scan ``text`` for commented-out ``# key = ...`` lines, grouped by section.

    Used to tell an *absent* key (report it / add it) apart from one an operator
    already has as a comment (leave it alone) — this keeps migration idempotent.
    """
    result: dict[str, set[str]] = {}
    section: str | None = None
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).strip()
            continue
        if section is None:
            continue
        cm = _COMMENTED_KEY_RE.match(line)
        if cm:
            result.setdefault(section, set()).add(cm.group(1))
    return result


def diff_config(text: str) -> ConfigReport:
    """Compare a config.toml body against the schema. Pure; no I/O.

    A key present only as a comment counts as "not missing" (the operator has
    already been shown it) but still uses its default at runtime.
    """
    report = ConfigReport()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        # Unparseable config: nothing actionable here; the loader surfaces it.
        return report

    known = file_known_keys()
    commented = commented_keys(text)

    # Known keys that are neither set nor mentioned as a comment.
    for section, keys in known.items():
        active = data.get(section)
        active = active if isinstance(active, dict) else {}
        seen_comment = commented.get(section, set())
        for key, default in keys.items():
            if key not in active and key not in seen_comment:
                report.missing.append((section, key, default))

    # Active keys/sections that are deprecated or unrecognized.
    for section, values in data.items():
        if not isinstance(values, dict):
            continue  # a bare top-level scalar; our config has none
        if section not in known:
            report.unknown_sections.append(section)
            continue
        for key in values:
            if (section, key) in DEPRECATED_KEYS:
                report.deprecated_present.append(
                    (section, key, DEPRECATED_KEYS[(section, key)])
                )
            elif key not in known[section] and (section, key) not in SECRET_KEYS:
                report.unknown_keys.append((section, key))
    return report


def check_config(config_dir: Path | None = None) -> ConfigReport:
    """Load ``config.toml`` from ``config_dir`` and diff it against the schema.

    Returns an empty report if the file is absent or unreadable.
    """
    config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    cfg_file = config_dir / "config.toml"
    try:
        text = cfg_file.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError):
        return ConfigReport()
    return diff_config(text)


# -- loading ------------------------------------------------------------------


def _read_secret(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, PermissionError):
        return ""


def load_config(config_dir: Path | None = None) -> Config:
    config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR

    data: dict = {}
    cfg_file = config_dir / "config.toml"
    if cfg_file.exists():
        with cfg_file.open("rb") as fh:
            data = tomllib.load(fh)

    server = ServerConfig(**_only_known(ServerConfig, data.get("server", {})))
    database = DatabaseConfig(**_only_known(DatabaseConfig, data.get("database", {})))
    logging_cfg = LoggingConfig(**_only_known(LoggingConfig, data.get("logging", {})))

    # The DB password lives ONLY in the secret file (or env), never in config.toml.
    password = _read_secret(config_dir / "secret") or os.environ.get("MCP_PG_DB_PASSWORD", "")
    if password:
        database.password = password

    token = _read_secret(config_dir / "token") or os.environ.get("MCP_PG_TOKEN", "")
    log_level = str(logging_cfg.level).upper()

    return Config(
        server=server,
        database=database,
        log_level=log_level,
        token=token,
        config_dir=config_dir,
    )
