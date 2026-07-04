---
name: run-tests-with-venv
description: Run pytest via the project .venv, not the system python on PATH
metadata:
  type: project
---

Run the suite with `.venv/Scripts/python.exe -m pytest tests/prod -q`. The system
`python` on PATH (D:\Python311) has neither `mcp` nor the editable `mcp_postgres`
install, so a bare `python -m pytest` fails at collection with
`ModuleNotFoundError`. The `.venv` at the repo root has both. Related:
[[bump-version-each-commit]].
