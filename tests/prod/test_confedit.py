"""Offline unit tests for the config-editing helpers.

These run on deploy without a DB or a live service, and cover the logic behind
the postgresql.conf / pg_hba.conf editing tools.
"""

from mcp_postgres.confedit import (
    append_hba_rule,
    format_value,
    is_shadowed_source,
    set_conf_value,
)


def test_format_value_quotes_strings():
    assert format_value("some string") == "'some string'"
    assert format_value("128MB") == "'128MB'"


def test_format_value_leaves_numbers_and_bools():
    assert format_value("100") == "100"
    assert format_value("1.5") == "1.5"
    assert format_value("on") == "on"
    assert format_value("off") == "off"


def test_format_value_escapes_quotes():
    assert format_value("a'b") == "'a''b'"


def test_set_conf_value_replaces_active_setting():
    content = "max_connections = 100\nshared_buffers = 128MB\n"
    r = set_conf_value(content, "max_connections", "200")
    assert r.changed
    assert r.action == "replaced"
    assert r.old_value == "100"
    assert r.active_occurrences == 1
    assert r.shadowed_disabled == 0
    assert "max_connections = 200" in r.content
    assert "shared_buffers = 128MB" in r.content


def test_set_conf_value_uncomments_commented_setting():
    content = "#work_mem = 4MB\n"
    r = set_conf_value(content, "work_mem", "8MB")
    assert r.changed
    assert r.action == "uncommented"
    assert r.old_value is None
    assert "work_mem = '8MB'" in r.content
    assert not r.content.strip().startswith("#work_mem")


def test_set_conf_value_appends_when_absent():
    content = "max_connections = 100\n"
    r = set_conf_value(content, "log_min_duration_statement", "500")
    assert r.changed
    assert r.action == "appended"
    assert "log_min_duration_statement = 500" in r.content


def test_set_conf_value_edits_the_effective_last_duplicate():
    # PostgreSQL uses the LAST active occurrence; editing the first (the old bug)
    # would leave the trailing line — and the running value — unchanged.
    content = (
        "shared_preload_libraries = 'pg_stat_statements'\n"
        "# ---- CUSTOMIZED OPTIONS ----\n"
        "shared_preload_libraries = 'timescaledb'\n"
    )
    r = set_conf_value(content, "shared_preload_libraries", "pg_stat_statements,timescaledb")
    assert r.changed
    assert r.action == "deduplicated"
    assert r.active_occurrences == 2
    assert r.shadowed_disabled == 1
    # old_value is the previously EFFECTIVE (last) value, not the first line's.
    assert r.old_value == "'timescaledb'"
    lines = r.content.splitlines()
    active = [ln for ln in lines if ln.strip().startswith("shared_preload_libraries")]
    # Exactly one live line remains, and it carries the new value.
    assert active == ["shared_preload_libraries = 'pg_stat_statements,timescaledb'"]
    # The earlier duplicate is commented out (disabled), not deleted.
    assert any(
        ln.lstrip().startswith("#") and "pg_stat_statements'" in ln and "disabled by mcp-postgres" in ln
        for ln in lines
    )


def test_set_conf_value_dedup_is_idempotent():
    content = "listen_addresses = 'localhost'\nlisten_addresses = '*'\n"
    first = set_conf_value(content, "listen_addresses", "*")
    assert first.changed and first.shadowed_disabled == 1
    # Re-running on the already-deduplicated file is a no-op.
    second = set_conf_value(first.content, "listen_addresses", "*")
    assert not second.changed
    assert second.action == "replaced"
    assert second.active_occurrences == 1
    assert second.shadowed_disabled == 0


def test_set_conf_value_noop_when_value_unchanged():
    content = "max_connections = 200\n"
    r = set_conf_value(content, "max_connections", "200")
    assert not r.changed


def test_set_conf_value_ignores_prefix_named_settings():
    # Setting `log_connections` must not be confused with `log_connections_extra`.
    content = "log_connections_extra = on\n"
    r = set_conf_value(content, "log_connections", "on")
    assert r.action == "appended"
    assert "log_connections_extra = on" in r.content
    assert "log_connections = on" in r.content


def test_is_shadowed_source():
    conf = "/etc/postgresql/16/main/postgresql.conf"
    # Effective value comes from auto.conf -> our postgresql.conf edit is shadowed.
    assert is_shadowed_source(conf, "/etc/postgresql/16/main/postgresql.auto.conf")
    # Same file (compared by basename, tolerating a relative sourcefile) -> not shadowed.
    assert not is_shadowed_source(conf, "postgresql.conf")
    assert not is_shadowed_source(conf, "/etc/postgresql/16/main/postgresql.conf")
    # No source at all (default / unset) -> not shadowed.
    assert not is_shadowed_source(conf, None)
    assert not is_shadowed_source(conf, "")


def test_append_hba_rule_appends_new():
    content = "local all all peer\n"
    rule = "host mydb myuser 127.0.0.1/32 scram-sha-256"
    new, changed = append_hba_rule(content, rule)
    assert changed
    assert rule in new


def test_append_hba_rule_is_idempotent():
    rule = "host mydb myuser 127.0.0.1/32 scram-sha-256"
    content = f"local all all peer\n{rule}\n"
    new, changed = append_hba_rule(content, rule)
    assert not changed
    assert new == content
