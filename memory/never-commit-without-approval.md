---
name: never-commit-without-approval
description: "Commits need explicit per-commit approval; never git push — the user always pushes manually"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: a010d46e-e1a1-4d66-8f89-924d5245f804
---

Never run `git commit` (or `git push`) until the user has clearly and explicitly approved that specific commit. Approval of the work/plan is NOT approval to commit.

**Why:** The user wants to review changes before they enter git history and be the one who decides when a commit happens. (Stated 2026-07-03, superseding the earlier project rule "commit always to main" which dictated the branch, not the timing.)

**How to apply:** After making and verifying edits, STOP and ask for confirmation before committing. Do not batch-commit proactively at the end of a task. When changes are ready, summarize them and ask "commit?" — only run `git commit` after a clear yes. Staging (`git add`) is fine to prepare, but do not commit. This applies to every commit individually, not once per session. Note this loosens the standing instruction to commit to main [[repo rule: commit to main]].

**Pushing is always the user's job.** The user pushes manually, every time (stated 2026-07-03). Never run `git push`, and do NOT offer to push or ask "want me to push?" after a commit — just report the commit and stop. Leaving local commits ahead of the remote is expected and fine.
