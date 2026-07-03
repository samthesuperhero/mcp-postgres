"""In-place migration of a deployed ``config.toml`` to the current schema.

Exposed as the ``mcp-postgres-migrate-config`` console script and invoked by
``update.py`` after the venv is rebuilt and before the service restarts. It is
**behavior-neutral**: newly-known keys are added *commented out* (so the loader
default still applies and an operator is never pinned to a default that may change
later), and deprecated keys are *commented out* rather than deleted. Effective
config is unchanged; the operator just gets a self-documenting, up-to-date file.

The text transforms are pure and side-effect free (like ``confedit.py``) so they
can be unit tested offline; ``main`` adds the file I/O, a timestamped backup, and
restores the file's mode/owner.
"""

from __future__ import annotations

import os
import re

from .config import diff_config

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _toml_literal(value: object) -> str:
    """Render a Python default as a TOML scalar."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _find_active_key_line(lines: list[str], section: str, key: str) -> int | None:
    """Index of the active (uncommented) ``key = ...`` line inside ``section``."""
    pat = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    cur: str | None = None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m:
            cur = m.group(1).strip()
            continue
        if cur == section and pat.match(line):
            return i
    return None


def _insert_into_section(lines: list[str], section: str, new_lines: list[str]) -> list[str]:
    """Insert ``new_lines`` at the end of ``section`` (appending the section if absent)."""
    header_idx: int | None = None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m and m.group(1).strip() == section:
            header_idx = i
            break

    if header_idx is None:
        out = list(lines)
        if out and out[-1].strip() != "":
            out.append("")
        out.append(f"[{section}]")
        out.extend(new_lines)
        return out

    end = len(lines)
    for j in range(header_idx + 1, len(lines)):
        if _SECTION_RE.match(lines[j]):
            end = j
            break
    # Insert before any trailing blank lines that belong to this section.
    insert_at = end
    while insert_at - 1 > header_idx and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    return lines[:insert_at] + new_lines + lines[insert_at:]


def apply_migration(text: str, version: str | None) -> tuple[str, list[str]]:
    """Migrate a config.toml body to the current schema. Pure; no I/O.

    Returns ``(new_text, changes)``. ``changes`` is empty (and ``new_text`` equals
    ``text``) when nothing needs migrating. Idempotent: a key already present as a
    comment is not re-added, and an already-commented deprecated key is left alone.
    """
    report = diff_config(text)
    if not report.missing and not report.deprecated_present:
        return text, []

    lines = text.splitlines()
    changes: list[str] = []

    # 1) Comment out deprecated, still-active keys.
    for section, key, reason in report.deprecated_present:
        idx = _find_active_key_line(lines, section, key)
        if idx is None:
            continue
        note = f"deprecated in {version}: {reason}" if version else f"deprecated: {reason}"
        lines[idx] = f"# {lines[idx]}   # {note}"
        changes.append(f"[{section}] {key}: commented out ({reason})")

    # 2) Add newly-known keys, commented, under their section.
    missing_by_section: dict[str, list[tuple[str, object]]] = {}
    for section, key, default in report.missing:
        missing_by_section.setdefault(section, []).append((key, default))

    for section, items in missing_by_section.items():
        new_lines = []
        for key, default in items:
            note = f"new in {version}" if version else "new setting"
            literal = _toml_literal(default)
            new_lines.append(f"# {key} = {literal}   # {note}; uncomment to override")
            changes.append(f"[{section}] {key}: added (commented, default {literal})")
        lines = _insert_into_section(lines, section, new_lines)

    if not changes:
        return text, []
    return "\n".join(lines) + "\n", changes


# -- entrypoint (deployed host only) -----------------------------------------


def _write(path, content: str, st) -> None:
    """Write ``content`` to ``path`` and restore ``st``'s mode/owner (best effort)."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    try:
        os.chmod(path, st.st_mode & 0o777)
        os.chown(path, st.st_uid, st.st_gid)
    except (PermissionError, AttributeError):
        pass  # e.g. dev host without privileges / non-POSIX


def main() -> None:
    from datetime import datetime

    from .config import DEFAULT_CONFIG_DIR
    from .docs import version as pkg_version

    cfg_file = DEFAULT_CONFIG_DIR / "config.toml"
    if not cfg_file.exists():
        print(f"[migrate-config] {cfg_file} not found — nothing to migrate")
        return

    try:
        text = cfg_file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[migrate-config] WARNING: cannot read {cfg_file}: {exc}")
        return

    new_text, changes = apply_migration(text, pkg_version())
    if not changes:
        print(f"[migrate-config] {cfg_file} is up to date — no changes")
        return

    try:
        st = os.stat(cfg_file)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = cfg_file.with_name(f"{cfg_file.name}.{stamp}.bak")
        _write(backup, text, st)  # keep the backup as tight as the original
        _write(cfg_file, new_text, st)
    except OSError as exc:
        print(f"[migrate-config] WARNING: could not migrate {cfg_file}: {exc}")
        return

    print(f"[migrate-config] backed up {cfg_file} -> {backup.name}")
    for change in changes:
        print(f"[migrate-config]   {change}")
    print(f"[migrate-config] migrated {cfg_file} ({len(changes)} change(s))")


if __name__ == "__main__":
    main()
