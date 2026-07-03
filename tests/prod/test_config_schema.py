"""Guard: the shipped config.toml.template must match the code's schema.

A fresh install renders packaging/config.toml.template; existing deployments are
migrated from the dataclass schema. If a developer adds a field to config.py but
forgets the template (or vice versa), fresh and migrated hosts drift apart. This
test fails loudly in that case.
"""

import re
from pathlib import Path

from mcp_postgres.config import file_known_keys

TEMPLATE = Path(__file__).resolve().parents[2] / "packaging" / "config.toml.template"

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_KEY_RE = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*=")


def _template_keys() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    section = None
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).strip()
            out.setdefault(section, set())
            continue
        if section is None or line.lstrip().startswith("#"):
            continue
        km = _KEY_RE.match(line)
        if km:
            out[section].add(km.group(1))
    return out


def test_template_matches_schema():
    schema = {section: set(keys) for section, keys in file_known_keys().items()}
    assert _template_keys() == schema
