"""Offline tests that the config-file tools pass the *discovered absolute path*
to the privhelper — not a bare basename (which the privhelper resolves against
its CWD, ``/`` under sudo, and rejects). No DB or sudo required.

The tools are nested closures registered via ``@mcp.tool``; a capturing stub MCP
grabs them so they can be invoked directly with a fake priv client and a stub
capability probe.
"""

import pytest

from mcp_postgres.capabilities import CapabilityError, DbTier, OsTier
from mcp_postgres.privclient import PrivClient, PrivError
from mcp_postgres.tools import config_files

CONFIG_FILE = "/data/pgdata/postgresql.conf"
HBA_FILE = "/data/pgdata/pg_hba.conf"


class _CapturingMCP:
    """Stand-in for FastMCP: ``@mcp.tool(...)`` just records the function."""

    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


class _FakePriv:
    def __init__(self, content="max_connections = 100\n"):
        self.content = content
        self.reads = []
        self.writes = []
        self.reloaded = False

    def read(self, path):
        self.reads.append(path)
        return self.content

    def write(self, path, content):
        self.writes.append((path, content))

    def reload(self):
        self.reloaded = True


class _StubCaps:
    def __init__(self, db_info, allow=True):
        self._db_info = db_info
        self.allow = allow

    def guard(self, os_min=None, db_min=None):
        if not self.allow:
            raise CapabilityError("denied", [])
        return []

    def db_info(self, force=False):
        return self._db_info

    def db_tier(self, force=False):
        return DbTier.DB_ADMIN

    def os_tier(self, force=False):
        return OsTier.OS_CONFIG


class _StubDb:
    def query_one(self, sql, params=None):
        # Mirror the real pg_settings probe: effective value comes from the
        # postgresql.conf we edit, so no shadow warning should fire.
        return {"context": "sighup", "sourcefile": CONFIG_FILE}


class _StubTarget:
    dbname = "postgres"

    def __init__(self, db_info):
        self.caps = _StubCaps(db_info)
        self.db = _StubDb()


class _StubManager:
    def __init__(self, target):
        self._target = target

    def current_target(self):
        return self._target


class _StubCtx:
    def __init__(self, priv, target):
        self.priv = priv
        self.manager = _StubManager(target)


def _register(db_info, content="max_connections = 100\n"):
    """Register the config tools and return (tools_dict, fake_priv)."""
    priv = _FakePriv(content)
    ctx = _StubCtx(priv, _StubTarget(db_info))
    mcp = _CapturingMCP()
    config_files.register(mcp, ctx)
    return mcp.tools, priv


_FULL = {"config_file": CONFIG_FILE, "hba_file": HBA_FILE}


def test_read_postgresql_conf_uses_discovered_path():
    tools, priv = _register(_FULL)
    result = tools["read_postgresql_conf"]()
    assert result["ok"] is True
    assert priv.reads == [CONFIG_FILE]  # full path, not "postgresql.conf"
    assert result["path"] == CONFIG_FILE
    assert result["file"] == "postgresql.conf"


def test_read_pg_hba_conf_uses_discovered_path():
    tools, priv = _register(_FULL)
    result = tools["read_pg_hba_conf"]()
    assert result["ok"] is True
    assert priv.reads == [HBA_FILE]
    assert result["path"] == HBA_FILE


def test_update_setting_reads_and_writes_discovered_path():
    tools, priv = _register(_FULL)
    result = tools["update_postgresql_setting"]("max_connections", "200")
    assert result["ok"] is True
    assert result["changed"] is True
    assert result["action"] == "replaced"
    assert priv.reads == [CONFIG_FILE]
    assert [p for p, _ in priv.writes] == [CONFIG_FILE]
    assert result["path"] == CONFIG_FILE
    # Single occurrence in the edited file → no duplicate/shadow warnings.
    assert "duplicates_disabled" not in result
    assert "note" not in result


def test_update_hba_rule_writes_discovered_path():
    tools, priv = _register(_FULL, content="local all all peer\n")
    rule = "host mydb myuser 127.0.0.1/32 scram-sha-256"
    result = tools["update_pg_hba_rule"](rule)
    assert result["ok"] is True
    assert priv.reads == [HBA_FILE]
    assert [p for p, _ in priv.writes] == [HBA_FILE]


def test_missing_config_path_is_a_clean_error():
    # DB unreachable → current_setting returned NULL → config_file is None.
    tools, priv = _register({"config_file": None, "hba_file": None})
    result = tools["read_postgresql_conf"]()
    assert result["ok"] is False
    assert "could not determine" in result["error"]
    assert priv.reads == []  # never shelled out to the privhelper


# -- app-layer basename allowlist in PrivClient --------------------------------


def test_privclient_rejects_disallowed_basename():
    # The guard runs before any sudo call, so a bogus helper path is never reached.
    client = PrivClient(helper="/nonexistent/privhelper")
    with pytest.raises(PrivError, match="not allowlisted"):
        client.read("/etc/passwd")
    with pytest.raises(PrivError, match="not allowlisted"):
        client.write("/etc/shadow", "x")
