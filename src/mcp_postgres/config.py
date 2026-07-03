"""Configuration loading for mcp-postgres.

Reads ``/etc/mcp-postgres/config.toml`` plus the ``secret`` (DB password) and
``token`` (MCP bearer token) files. The config directory can be overridden with
the ``MCP_PG_CONFIG_DIR`` environment variable (used by tests). Secrets may also
be supplied via ``MCP_PG_DB_PASSWORD`` / ``MCP_PG_TOKEN`` when no file exists.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

DEFAULT_CONFIG_DIR = Path(os.environ.get("MCP_PG_CONFIG_DIR", "/etc/mcp-postgres"))


def _only_known(cls, data: dict) -> dict:
    """Drop unexpected keys so a stray config entry can't crash startup."""
    known = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in known}


@dataclass
class ServerConfig:
    bind: str = "127.0.0.1"
    port: int = 8080
    path: str = "/mcp"


@dataclass
class DatabaseConfig:
    host: str = "127.0.0.1"
    port: int = 5432
    user: str = "mcp"
    dbname: str = "postgres"
    password: str = ""


@dataclass
class Config:
    server: ServerConfig
    database: DatabaseConfig
    log_level: str = "INFO"
    token: str = ""
    config_dir: Path = DEFAULT_CONFIG_DIR


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

    # The DB password lives ONLY in the secret file (or env), never in config.toml.
    password = _read_secret(config_dir / "secret") or os.environ.get("MCP_PG_DB_PASSWORD", "")
    if password:
        database.password = password

    token = _read_secret(config_dir / "token") or os.environ.get("MCP_PG_TOKEN", "")
    log_level = str(data.get("logging", {}).get("level", "INFO")).upper()

    return Config(
        server=server,
        database=database,
        log_level=log_level,
        token=token,
        config_dir=config_dir,
    )
