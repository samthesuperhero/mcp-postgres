"""Offline unit tests for config schema drift detection and migration.

These run on deploy without a DB or a live service. They cover config.diff_config
and configmigrate.apply_migration — the logic behind the in-place config.toml
migration the updater performs.
"""

import tomllib

import pytest

import mcp_postgres.config as config
from mcp_postgres.config import diff_config
from mcp_postgres.configmigrate import _toml_literal, apply_migration

# A config that exactly matches the real (unpatched) schema.
COMPLETE = """\
[server]
bind = "127.0.0.1"
port = 41780
path = "/mcp"

[database]
host = "127.0.0.1"
port = 5432
user = "mcp"
dbname = "postgres"

[logging]
level = "INFO"

[oauth]
enabled = false
public_url = ""
access_token_ttl = 3600
refresh_token_ttl = 2592000
state_dir = "/var/lib/mcp-postgres"
"""


@pytest.fixture
def add_new_key(monkeypatch):
    """Pretend the schema grew a ``database.connect_timeout`` key (default 5)."""
    known = config.file_known_keys()
    known["database"] = {**known["database"], "connect_timeout": 5}
    monkeypatch.setattr(config, "file_known_keys", lambda: known)
    return known


@pytest.fixture
def add_deprecation(monkeypatch):
    """Pretend ``server.tls`` was removed."""
    monkeypatch.setattr(
        config, "DEPRECATED_KEYS", {("server", "tls"): "removed in 0.3.0; nginx terminates TLS"}
    )


# -- diff_config --------------------------------------------------------------


def test_complete_config_has_no_drift():
    assert not diff_config(COMPLETE).any()


def test_diff_classifies_missing_deprecated_unknown(add_new_key, add_deprecation):
    cfg = COMPLETE.replace('path = "/mcp"', 'path = "/mcp"\ntls = true')
    cfg = cfg.replace('dbname = "postgres"', 'dbname = "postgres"\ntypo_key = 1')
    report = diff_config(cfg)
    assert ("database", "connect_timeout", 5) in report.missing
    assert ("server", "tls", "removed in 0.3.0; nginx terminates TLS") in report.deprecated_present
    assert ("database", "typo_key") in report.unknown_keys


def test_unknown_section_reported():
    report = diff_config(COMPLETE + "\n[bogus]\nx = 1\n")
    assert "bogus" in report.unknown_sections


def test_unparseable_config_returns_empty_report():
    assert not diff_config("this is not = = toml [[[").any()


def test_commented_key_counts_as_present(add_new_key):
    cfg = COMPLETE.replace(
        'dbname = "postgres"', 'dbname = "postgres"\n# connect_timeout = 5'
    )
    assert ("database", "connect_timeout", 5) not in diff_config(cfg).missing


# -- apply_migration ----------------------------------------------------------


def test_complete_config_is_noop():
    new, changes = apply_migration(COMPLETE, "0.3.0")
    assert changes == []
    assert new == COMPLETE


def test_missing_key_added_commented_and_behavior_neutral(add_new_key):
    new, changes = apply_migration(COMPLETE, "0.3.0")
    assert any("connect_timeout" in c for c in changes)
    # Added commented (not active): effective config is unchanged.
    assert "# connect_timeout = 5" in new
    assert tomllib.loads(new)["database"] == tomllib.loads(COMPLETE)["database"]


def test_missing_key_added_under_correct_section(add_new_key):
    new, _ = apply_migration(COMPLETE, "0.3.0")
    db_start = new.index("[database]")
    log_start = new.index("[logging]")
    assert db_start < new.index("# connect_timeout = 5") < log_start


def test_missing_section_is_appended():
    cfg = COMPLETE[: COMPLETE.index("[logging]")].rstrip() + "\n"
    new, changes = apply_migration(cfg, "0.3.0")
    assert "[logging]" in new
    assert '# level = "INFO"' in new
    assert any("level" in c for c in changes)


def test_deprecated_key_commented_out(add_deprecation):
    cfg = COMPLETE.replace('path = "/mcp"', 'path = "/mcp"\ntls = true')
    new, changes = apply_migration(cfg, "0.3.0")
    assert '# tls = true' in new
    assert "deprecated in 0.3.0" in new
    assert tomllib.loads(new).get("server", {}).get("tls") is None  # no longer active
    assert any("tls" in c for c in changes)


def test_unknown_key_left_untouched(add_deprecation):
    cfg = COMPLETE.replace('dbname = "postgres"', 'dbname = "postgres"\ntypo_key = 1')
    new, _ = apply_migration(cfg, "0.3.0")
    assert tomllib.loads(new)["database"]["typo_key"] == 1  # still active


def test_migration_is_idempotent(add_new_key, add_deprecation):
    cfg = COMPLETE.replace('path = "/mcp"', 'path = "/mcp"\ntls = true')
    once, changes1 = apply_migration(cfg, "0.3.0")
    assert changes1
    twice, changes2 = apply_migration(once, "0.3.0")
    assert changes2 == []
    assert twice == once


def test_password_is_never_added():
    # password is a DatabaseConfig field but a SECRET_KEY: it must never be a
    # migration target, even for a config that omits it.
    assert ("database", "password") not in [(s, k) for s, k, _ in diff_config(COMPLETE).missing]
    new, _ = apply_migration(COMPLETE, "0.3.0")
    assert "password" not in new


def test_version_none_uses_generic_annotation(add_new_key):
    new, _ = apply_migration(COMPLETE, None)
    assert "new setting" in new
    assert "new in" not in new


# -- _toml_literal ------------------------------------------------------------


def test_toml_literal_renders_scalars():
    assert _toml_literal("mcp") == '"mcp"'
    assert _toml_literal(5432) == "5432"
    assert _toml_literal(True) == "true"
    assert _toml_literal(False) == "false"
    assert _toml_literal('a"b') == '"a\\"b"'
