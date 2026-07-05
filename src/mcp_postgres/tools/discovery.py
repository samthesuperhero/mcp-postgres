"""MCP Resources that let an agent discover the service without prior knowledge.

Two static resources complement the ``initialize`` instructions (server.py) and the
per-tool annotations:

* ``docs://mcp-postgres/guide``   — the full capability guide (Markdown).
* ``capabilities://current``      — the live capability report (JSON), the same
  payload as the ``get_capabilities`` tool, so agents that prefer resource discovery
  can read current tiers/enabled tools without a tool call.
* ``schema://current``            — a compact structural map of the current target
  database (schemas → tables/views with columns, primary keys, FK edges, and enums),
  so an agent can orient itself in one read instead of many ``describe_table`` calls.
"""

from __future__ import annotations

import json

from ..docs import CAPABILITIES_URI, GUIDE_MARKDOWN, GUIDE_URI, SCHEMA_URI
from .introspection import collect_db_schema


def register(mcp, ctx) -> None:
    @mcp.resource(
        GUIDE_URI,
        name="mcp-postgres guide",
        title="mcp-postgres capability guide",
        description="What the service can do, the privilege-tier model, the result envelope, and the tool catalog.",
        mime_type="text/markdown",
    )
    def guide() -> str:
        return GUIDE_MARKDOWN

    @mcp.resource(
        CAPABILITIES_URI,
        name="current capabilities",
        title="Live capability report",
        description="Current OS/DB tiers, connected role, and the tools enabled right now (JSON).",
        mime_type="application/json",
    )
    def current_capabilities() -> str:
        t = ctx.manager.current_target()
        return json.dumps(t.caps.report(database=t.dbname), indent=2)

    @mcp.resource(
        SCHEMA_URI,
        name="current database schema",
        title="Current database schema",
        description="Compact map of the current target database — every non-system schema with "
        "its tables/views (columns, primary key, FK edges) and enum types (JSON).",
        mime_type="application/json",
    )
    def current_schema() -> str:
        t = ctx.manager.current_target()
        try:
            data = collect_db_schema(t.db)
        except Exception as exc:  # noqa: BLE001 - a resource must still return legible content
            data = {"database": t.dbname, "error": str(exc)}
        return json.dumps(data, indent=2, default=str)
