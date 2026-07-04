"""Pure text helpers for editing PostgreSQL config files.

These functions have no side effects (no file or DB access) so they can be unit
tested directly as part of the on-deploy test suite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")
_BOOLISH = {"on", "off", "true", "false", "yes", "no"}

# Marker appended to a duplicate line we neutralize, so the change is auditable
# in the file itself (and so a human knows we did not delete their value).
_SHADOW_MARKER = "disabled by mcp-postgres: shadowed duplicate"


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


@dataclass(frozen=True)
class SetConfResult:
    """Outcome of :func:`set_conf_value`.

    ``old_value`` is the previous *effective* value (the last active occurrence,
    which is the one PostgreSQL actually used), or ``None`` when the setting was
    only commented out or entirely absent. ``active_occurrences`` counts how many
    uncommented lines for the setting existed before the edit; ``shadowed_disabled``
    counts how many earlier duplicates were commented out to leave a single
    unambiguous line.
    """

    content: str
    changed: bool
    old_value: str | None
    action: str  # "replaced" | "deduplicated" | "uncommented" | "appended"
    active_occurrences: int
    shadowed_disabled: int


def set_conf_value(content: str, name: str, value: str) -> SetConfResult:
    """Set ``name = value`` in a postgresql.conf body, duplicate-safe.

    PostgreSQL honours the *last* uncommented occurrence of a setting, so editing
    the first one (as a naive replace would) can silently fail to change the value
    when the file contains duplicates. This function therefore:

    1. updates the last active (uncommented) occurrence — the one in effect — so the
       change actually takes hold, and comments out any earlier active duplicates so
       the file is left with exactly one live line for the setting; failing that,
    2. un-comments the last commented occurrence; failing that,
    3. appends a new line.

    Earlier duplicates are commented out (not deleted), keeping the edit reversible
    and auditable — the privhelper also writes a timestamped backup of every write.
    """
    formatted = format_value(value)
    new_line = f"{name} = {formatted}"
    pat = re.compile(r"^(\s*)(#+\s*)?" + re.escape(name) + r"\s*=", re.IGNORECASE)

    lines = content.splitlines()
    original = _norm(content)

    active: list[int] = []
    commented: list[int] = []
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        (commented if m.group(2) else active).append(i)

    # Pass 1: there is at least one live setting — update the effective (last) one
    # and neutralize any earlier duplicates that were shadowing nothing anyway.
    if active:
        winner = active[-1]
        old_value = lines[winner].split("=", 1)[1].strip()
        lines[winner] = new_line
        shadowed = active[:-1]
        for i in shadowed:
            lines[i] = _comment_out(lines[i])
        new_content = "\n".join(lines) + "\n"
        return SetConfResult(
            content=new_content,
            changed=new_content != original,
            old_value=old_value,
            action="deduplicated" if shadowed else "replaced",
            active_occurrences=len(active),
            shadowed_disabled=len(shadowed),
        )

    # Pass 2: only commented occurrences — un-comment the last one.
    if commented:
        lines[commented[-1]] = new_line
        new_content = "\n".join(lines) + "\n"
        return SetConfResult(
            content=new_content,
            changed=new_content != original,
            old_value=None,
            action="uncommented",
            active_occurrences=0,
            shadowed_disabled=0,
        )

    # Pass 3: append.
    lines.append(new_line)
    new_content = "\n".join(lines) + "\n"
    return SetConfResult(
        content=new_content,
        changed=new_content != original,
        old_value=None,
        action="appended",
        active_occurrences=0,
        shadowed_disabled=0,
    )


def is_shadowed_source(edited_path: str, sourcefile: str | None) -> bool:
    """True if the effective value comes from a file other than the one edited.

    ``pg_settings.sourcefile`` names the file that supplied the value currently in
    effect. If that is not the file we just edited — e.g. ``postgresql.auto.conf``
    written by ``ALTER SYSTEM``, or a later ``include`` — the edit is shadowed and
    will not take effect until that other file is changed. Compared by basename so a
    relative ``sourcefile`` (resolved by PostgreSQL against the data directory) still
    matches the absolute path we hold.
    """
    if not sourcefile:
        return False
    return _basename(sourcefile) != _basename(edited_path)


def append_hba_rule(content: str, rule: str) -> tuple[str, bool]:
    """Append a pg_hba.conf rule line if an identical line isn't already present."""
    rule = rule.strip()
    existing = {ln.strip() for ln in content.splitlines()}
    if rule in existing:
        return content, False
    base = content if content.endswith("\n") else content + "\n"
    return base + rule + "\n", True


def _comment_out(line: str) -> str:
    """Comment out an active setting line, preserving indentation and tagging why."""
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]
    return f"{indent}# {stripped}    # {_SHADOW_MARKER}"


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _norm(content: str) -> str:
    return content if content.endswith("\n") else content + "\n"
