"""Shared application context passed to tool-registration functions."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .manager import DatabaseManager
from .privclient import PrivClient


@dataclass
class AppContext:
    config: Config
    manager: DatabaseManager
    priv: PrivClient
