"""Shared application context passed to tool-registration functions."""

from __future__ import annotations

from dataclasses import dataclass, field

from .capabilities import CapabilityManager
from .config import Config
from .db import Database
from .privclient import PrivClient


@dataclass
class AppContext:
    config: Config
    db: Database
    caps: CapabilityManager
    priv: PrivClient
    enabled_tools: list[str] = field(default_factory=list)
