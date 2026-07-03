---
name: no-crlf-lf-warnings
description: "Never let \"LF will be replaced by CRLF\" git warnings appear; pin LF via .gitattributes"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: a010d46e-e1a1-4d66-8f89-924d5245f804
---

The user requires that the git warning "LF will be replaced by CRLF the next time Git touches it" must NEVER appear.

**Why:** The host is Windows with global `core.autocrlf=true`, but this project deploys to Linux (systemd unit, sudoers, `privhelper` shebang, shell-invoked Python) where CRLF would break things. The warnings are noise and signal a real line-ending risk.

**How to apply:** The repo has a `.gitattributes` (committed 60b328e) that pins LF for all text files (`* text=auto eol=lf` plus explicit per-type rules) — this overrides the global `core.autocrlf` per-repo. Keep it; if new file types are added and warnings reappear, add an `eol=lf` rule and run `git add --renormalize .`. Never commit files with CRLF. Do not rely on changing the user's global git config.
