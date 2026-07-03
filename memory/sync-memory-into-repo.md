---
name: sync-memory-into-repo
description: "Always mirror project memory into the repo's memory/ dir in the same commit — never ask"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 57ce7f77-dc41-41f8-b9a5-b041def9838a
---

The repo keeps a version-controlled copy of the project memory under `memory/` (started in commit ca3a848). Whenever any memory file in the working store (`~/.claude/projects/E--dev-mcp-postgres/memory/`) is created or changed, **copy the whole memory set into the repo's `memory/` dir and stage it as part of the same commit** as the related work. Do this automatically — do NOT ask whether to sync.

**Why:** The user wants the repo's `memory/` to always be current so it travels with the code (other machines / clones) and is reviewable in history. (Stated 2026-07-03.)

**How to apply:** After writing/editing any working-store memory file, mirror it to `E:\dev\mcp-postgres\memory\` (files are LF — [[no-crlf-lf-warnings]]) and `git add memory/` alongside the code changes so the memory lands in the same commit. Still honor per-commit approval [[never-commit-without-approval]] — the syncing is automatic, the commit itself is not.
