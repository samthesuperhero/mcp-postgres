---
name: mcp-postgres-roadmap
description: The mcp-postgres feature roadmap and how far it has been implemented
metadata:
  type: project
---

The mcp-postgres development roadmap was defined in the plan
`~/.claude/plans/propose-extension-of-mcp-postgres-lazy-cray.md`. Status as of 2026-07-05:

- **Phase 1 — richer introspection + safer/smarter queries:** shipped **v0.9.0** (enriched
  `describe_table`, `explain_query`, `sample_table`, `execute_batch`, `run_read_query` timeouts).
- **Phase 2 — observability & ops:** implemented at **v0.10.0** — `server_activity`, `list_locks`,
  `database_stats`, `get_settings` (DB_READONLY); `cancel_query`, `terminate_backend` (DB_ADMIN).
  Module `tools/observability.py`.
- **Phase 3 — structure & guidance:** implemented at **v0.11.0** — MCP prompts
  (`audit_privileges`, `add_column_safely`, `investigate_slow_query`) in `tools/prompts.py`, plus
  the `schema://current` resource backed by `introspection.collect_db_schema`.
- **Phase 4 — backups/export (`pg_dump`/`pg_restore`):** deliberately OUT OF SCOPE — would widen
  the privhelper's OS allowlist beyond the two config files.
- **Backlog (not done):** trim `get_capabilities.environment.extensions.available` (50+ rows in
  every report) down to a count, moving the full list behind a `list_extensions` tool. Deferred by
  the user on 2026-07-05.

**Prod validation (2026-07-05):** at **v0.11.1** the full live prod-DB suite — **201 tests** —
passes on the deployment host (PostgreSQL, Python 3.13), exercising Phase 2 observability/ops and
Phase 3 prompts + `schema://current` end-to-end against the real cluster. Two live-only bugs found
and fixed in v0.11.1: a literal `%` colliding with a psycopg placeholder in `database_stats`, and
`get_settings` treating a name prefix's `_` as a LIKE wildcard.

**Why:** lets a future "propose next steps" pick up without re-discovering the roadmap.
**How to apply:** next feature increment is the backlog trim (small) or a new phase; follow
[[bump-version-each-commit]] and land each phase as its own commit.
