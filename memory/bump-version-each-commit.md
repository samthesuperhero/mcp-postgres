---
name: bump-version-each-commit
description: Bump the pyproject.toml version before every commit — minor vs patch is my call
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 57ce7f77-dc41-41f8-b9a5-b041def9838a
---

Before **every** commit, bump `version` in `pyproject.toml`. Choosing **minor** vs **patch** is at my own discretion (stated 2026-07-03): treat a new/changed user-visible behaviour or feature as a **minor** bump, and a bug fix / docs / chore as a **patch**. Never leave the version unchanged across a commit.

**Why:** The user wants every commit to carry a distinct, meaningful version so deploys are traceable and the running service advertises exactly which build it is.

**How to apply:** As the last step before staging a commit, edit `pyproject.toml` `version`. The service surfaces this value at runtime via `docs.version()` → `importlib.metadata` (in `get_capabilities` / `capabilities://current`), so after a venv reinstall the bumped version is visible as a deploy sanity check. Bundle the bump into the same commit as the change. Also remember the related rules: memory rides along in each commit ([[sync-memory-into-repo]]) and commits still need explicit approval ([[never-commit-without-approval]]).
