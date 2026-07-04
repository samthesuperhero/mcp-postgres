"""Live environment self-advertisement against the real cluster.

``environment.probe`` tells an agent *what it is talking to* — PostgreSQL version,
extensions, host OS. These caches are process-global, so the tests reset them to
observe a fresh probe of the real server rather than a value another test seeded.
"""

from __future__ import annotations

import pytest

from mcp_postgres import environment as env

pytestmark = pytest.mark.usefixtures("_live")


@pytest.fixture(autouse=True)
def _fresh_env_caches(monkeypatch):
    # server_version / host_os cache in module globals for the process lifetime.
    monkeypatch.setattr(env, "_version_cache", None)
    monkeypatch.setattr(env, "_os_cache", None)


def test_server_version_real(live_db):
    info = env.server_version(live_db)
    assert isinstance(info["version_num"], int) and info["version_num"] > 0
    assert info["version_string"].startswith("PostgreSQL")
    # major is version_num // 10000 (e.g. 160003 -> 16).
    assert info["major"] == info["version_num"] // 10000
    assert str(info["major"]) in info["server_version"]


def test_extensions_shape(live_db):
    exts = env.extensions(live_db)
    assert set(exts) == {"activated", "available", "preloaded_libraries"}
    assert isinstance(exts["activated"], list)
    assert isinstance(exts["available"], list)
    assert isinstance(exts["preloaded_libraries"], list)
    # plpgsql is created in every stock database.
    activated_names = {e["name"] for e in exts["activated"]}
    assert "plpgsql" in activated_names
    for e in exts["activated"]:
        assert e["name"] and e["version"]
    for e in exts["available"]:
        assert e["name"]  # default_version may be null for some, but the name is set


def test_extensions_partition_is_disjoint(live_db):
    exts = env.extensions(live_db)
    activated = {e["name"] for e in exts["activated"]}
    available = {e["name"] for e in exts["available"]}
    # An extension is either created here or merely available — never both.
    assert activated.isdisjoint(available)


def test_probe_composes_os_and_postgresql(live_db):
    e = env.probe(live_db)
    assert set(e) >= {"os", "postgresql"}
    assert "error" not in e["os"]
    assert "error" not in e["postgresql"]
    assert e["postgresql"]["version_num"] > 0
    assert "extensions" not in e["postgresql"] or "error" not in e["postgresql"]["extensions"]
    assert e["os"]["family"]  # 'Linux' on the deployment host


def test_host_os_reports_family():
    info = env.host_os()
    assert info["family"]  # non-empty OS family
    assert set(info) >= {"family", "name", "version", "pretty_name", "kernel", "arch"}
