"""Pure text helpers for editing PostgreSQL config files.

These functions have no side effects (no file or DB access) so they can be unit
tested directly as part of the on-deploy test suite.
"""

from __future__ import annotations

import re

_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")
_BOOLISH = {"on", "off", "true", "false", "yes", "no"}


def format_value(value: str) -> str:
    """Quote a postgresql.conf value unless it is numeric, boolean, or already quoted."""
    v = str(value).strip()
    if not v:
        return "''"
    if v.startswith("'") and v.endswith("'"):
        return v
    if _NUMERIC.match(v) or v.lower() in _BOOLISH:
        return v
    return "'" + v.replace("'", "''") + "'"


def set_conf_value(content: str, name: str, value: str) -> tuple[str, bool, str | None]:
    """Set ``name = value`` in a postgresql.conf body.

    Prefers replacing an existing uncommented setting; failing that, un-comments
    and replaces a commented one; failing that, appends a new line.

    Returns ``(new_content, changed, old_value)``.
    """
    formatted = format_value(value)
    new_line = f"{name} = {formatted}"
    pat = re.compile(r"^(\s*)(#\s*)?" + re.escape(name) + r"\s*=", re.IGNORECASE)

    lines = content.splitlines()
    old_value: str | None = None

    # Pass 1: replace the first active (uncommented) setting.
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m and not m.group(2):
            old_value = line.split("=", 1)[1].strip()
            lines[i] = new_line
            new_content = "\n".join(lines) + "\n"
            return new_content, new_content != _norm(content), old_value

    # Pass 2: un-comment and replace the first commented setting.
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m and m.group(2):
            lines[i] = new_line
            new_content = "\n".join(lines) + "\n"
            return new_content, new_content != _norm(content), None

    # Pass 3: append.
    lines.append(new_line)
    new_content = "\n".join(lines) + "\n"
    return new_content, new_content != _norm(content), None


def append_hba_rule(content: str, rule: str) -> tuple[str, bool]:
    """Append a pg_hba.conf rule line if an identical line isn't already present."""
    rule = rule.strip()
    existing = {ln.strip() for ln in content.splitlines()}
    if rule in existing:
        return content, False
    base = content if content.endswith("\n") else content + "\n"
    return base + rule + "\n", True


def _norm(content: str) -> str:
    return content if content.endswith("\n") else content + "\n"
