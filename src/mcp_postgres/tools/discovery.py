"""MCP Resources that let an agent discover the service without prior knowledge.

Two static resources complement the ``initialize`` instructions (server.py) and the
per-tool annotations:

* ``docs://mcp-postgres/guide``   — the full capability guide (Markdown).
* ``capabilities://current``      — the live capability report (JSON), the same
  payload as the ``get_capabilities`` tool, so agents that prefer resource discovery
  can read current tiers/enabled tools without a tool call.
"""

from __future__ import annotations

import json

from ..docs import CAPABILITIES_URI, GUIDE_MARKDOWN, GUIDE_URI


def register(mcp, ctx) -> None:
    caps = ctx.caps

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
        return json.dumps(caps.report(ctx.enabled_tools), indent=2)
