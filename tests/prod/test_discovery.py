"""Offline tests for MCP self-advertisement.

No DB or live service required: the tool modules are registered against a stub
context (lowest tiers, so the gated ``enabled_tools`` branches are skipped while
every tool is still decorated), and the resulting FastMCP tool/resource metadata
is inspected directly.
"""

import asyncio

from mcp.server.fastmcp import FastMCP

from mcp_postgres import docs
from mcp_postgres.capabilities import DbTier, OsTier
from mcp_postgres.tools import admin, config_files, discovery, introspection, query


class _StubCaps:
    def db_tier(self, force=False):
        return DbTier.DB_READONLY

    def os_tier(self, force=False):
        return OsTier.OS_NONE

    def report(self, enabled_tools):
        return {"service": "mcp-postgres", "enabled_tools": sorted(enabled_tools)}


class _StubCtx:
    def __init__(self):
        self.caps = _StubCaps()
        self.db = None
        self.priv = None
        self.enabled_tools = []


def _build() -> FastMCP:
    mcp = FastMCP("mcp-postgres-test")
    ctx = _StubCtx()
    for mod in (introspection, query, admin, config_files, discovery):
        mod.register(mcp, ctx)
    return mcp


READ_ONLY = {
    "get_capabilities",
    "health_check",
    "list_databases",
    "list_schemas",
    "list_tables",
    "describe_table",
    "run_read_query",
    "read_postgresql_conf",
    "read_pg_hba_conf",
}
DESTRUCTIVE = {"execute_sql", "admin_sql", "revoke"}


def test_instructions_and_guide_are_populated():
    assert "get_capabilities" in docs.SERVER_INSTRUCTIONS
    assert "DB_READONLY" in docs.SERVER_INSTRUCTIONS
    assert docs.GUIDE_URI in docs.SERVER_INSTRUCTIONS
    assert docs.CAPABILITIES_URI in docs.SERVER_INSTRUCTIONS
    assert "tool catalog" in docs.GUIDE_MARKDOWN.lower()
    assert docs.REPO_URL in docs.GUIDE_MARKDOWN


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
