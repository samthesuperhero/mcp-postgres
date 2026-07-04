"""Offline tests for MCP self-advertisement.

No DB or live service required: the tool modules are registered against a stub
context (lowest tiers, so the gated ``enabled_tools`` branches are skipped while
every tool is still decorated), and the resulting FastMCP tool/resource metadata
is inspected directly.
"""

import asyncio

from mcp.server.fastmcp import FastMCP

from mcp_postgres import docs
from mcp_postgres.capabilities import DbTier, OsTier, enabled_tools_for
from mcp_postgres.tools import admin, config_files, discovery, introspection, query, schema


class _StubCaps:
    def db_tier(self, force=False):
        return DbTier.DB_READONLY

    def os_tier(self, force=False):
        return OsTier.OS_NONE

    def report(self, database=None):
        return {
            "service": "mcp-postgres",
            "database": database,
            "enabled_tools": enabled_tools_for(OsTier.OS_NONE, DbTier.DB_READONLY),
        }


class _StubTarget:
    dbname = "postgres"

    def __init__(self):
        self.caps = _StubCaps()
        self.db = None


class _StubManager:
    def __init__(self):
        self._target = _StubTarget()
        self.current = "postgres"

    def current_target(self):
        return self._target

    def use(self, name):
        return self._target


class _StubCtx:
    def __init__(self):
        self.manager = _StubManager()
        self.priv = None


def _build() -> FastMCP:
    mcp = FastMCP("mcp-postgres-test")
    ctx = _StubCtx()
    for mod in (introspection, schema, query, admin, config_files, discovery):
        mod.register(mcp, ctx)
    return mcp


READ_ONLY = {
    "get_capabilities",
    "use_database",
    "health_check",
    "list_databases",
    "list_schemas",
    "list_tables",
    "describe_table",
    "list_foreign_keys",
    "list_indexes",
    "list_views",
    "list_functions",
    "list_enums",
    "get_object_definition",
    "run_read_query",
    "explain_query",
    "sample_table",
    "read_postgresql_conf",
    "read_pg_hba_conf",
}
DESTRUCTIVE = {"execute_sql", "execute_batch", "admin_sql", "revoke"}


def test_instructions_and_guide_are_populated():
    assert "get_capabilities" in docs.SERVER_INSTRUCTIONS
    assert "DB_READONLY" in docs.SERVER_INSTRUCTIONS
    assert docs.GUIDE_URI in docs.SERVER_INSTRUCTIONS
    assert docs.CAPABILITIES_URI in docs.SERVER_INSTRUCTIONS
    assert "tool catalog" in docs.GUIDE_MARKDOWN.lower()
    assert docs.REPO_URL in docs.GUIDE_MARKDOWN
    assert "STAY WITHIN THESE TOOLS" in docs.SERVER_INSTRUCTIONS
    assert "bypass" in docs.GUIDE_MARKDOWN.lower()


def test_every_tool_has_title_and_annotations():
    tools = {t.name: t for t in asyncio.run(_build().list_tools())}
    # Sanity: the whole catalog registered regardless of tier.
    assert READ_ONLY | DESTRUCTIVE <= set(tools)
    for name, tool in tools.items():
        assert tool.title, f"{name} is missing a title"
        assert tool.annotations is not None, f"{name} is missing annotations"
        assert tool.annotations.openWorldHint is False, f"{name} should be closed-world"


def test_read_only_and_destructive_hints():
    tools = {t.name: t for t in asyncio.run(_build().list_tools())}
    for name in READ_ONLY:
        assert tools[name].annotations.readOnlyHint is True, name
    for name in DESTRUCTIVE:
        assert tools[name].annotations.readOnlyHint is False, name
        assert tools[name].annotations.destructiveHint is True, name


def test_discovery_resources_registered():
    resources = asyncio.run(_build().list_resources())
    uris = {str(r.uri).rstrip("/") for r in resources}
    assert docs.GUIDE_URI.rstrip("/") in uris
    assert docs.CAPABILITIES_URI.rstrip("/") in uris
