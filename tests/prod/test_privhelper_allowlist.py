"""Tests for the privhelper allowlist logic (imported directly, no sudo needed)."""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load_privhelper():
    spec = importlib.util.spec_from_file_location("privhelper", REPO / "privhelper.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["privhelper"] = mod
    spec.loader.exec_module(mod)
    return mod


privhelper = _load_privhelper()


def test_allowed_basenames():
    assert privhelper.ALLOWED_BASENAMES == frozenset({"postgresql.conf", "pg_hba.conf"})


def test_resolve_allowed_rejects_disallowed(tmp_path):
    bad = tmp_path / "passwd"
    bad.write_text("x")
    with pytest.raises(SystemExit):
        privhelper.resolve_allowed(str(bad))


def test_resolve_allowed_accepts_postgresql_conf(tmp_path):
    good = tmp_path / "postgresql.conf"
    good.write_text("max_connections = 100\n")
    resolved = privhelper.resolve_allowed(str(good))
    assert resolved.endswith("postgresql.conf")


def test_resolve_allowed_rejects_missing_when_required(tmp_path):
    missing = tmp_path / "pg_hba.conf"
    with pytest.raises(SystemExit):
        privhelper.resolve_allowed(str(missing), must_exist=True)


def test_resolve_allowed_rejects_relative_path():
    # A bare basename must be refused, not silently resolved against CWD.
    with pytest.raises(SystemExit):
        privhelper.resolve_allowed("postgresql.conf")
