---
name: default-endpoint
description: "mcp-postgres default endpoint is http://127.0.0.1:41780/mcp, fronted by an nginx reverse proxy"
metadata: 
  node_type: memory
  type: project
  originSessionId: 57ce7f77-dc41-41f8-b9a5-b041def9838a
---

The mcp-postgres service listens on **`127.0.0.1:41780`** by default (MCP Streamable HTTP at the `/mcp` path → `http://127.0.0.1:41780/mcp`). In the standard deployment it runs **behind an nginx reverse proxy** that terminates TLS and is the only public entry point; the app port itself stays bound to localhost and is never exposed directly. Remote agents connect via the nginx URL (e.g. `https://<host>/mcp`); on-host clients can hit `127.0.0.1:41780` directly.

Defaults live in `src/mcp_postgres/config.py` (`ServerConfig.port = 41780`) and `install.py` (`--port` default). Changed from the earlier `8080` on 2026-07-03. When editing docs/examples, use `41780`, not `8080`.
