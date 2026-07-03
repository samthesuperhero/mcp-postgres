"""Offline unit tests for the config-editing helpers.

These run on deploy without a DB or a live service, and cover the logic behind
the postgresql.conf / pg_hba.conf editing tools.
"""

from mcp_postgres.confedit import append_hba_rule, format_value, set_conf_value


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
    new, changed, old = set_conf_value(content, "max_connections", "200")
    assert changed
    assert old == "100"
    assert "max_connections = 200" in new
    assert "shared_buffers = 128MB" in new


def test_set_conf_value_uncomments_commented_setting():
    content = "#work_mem = 4MB\n"
    new, changed, old = set_conf_value(content, "work_mem", "8MB")
    assert changed
    assert old is None
    assert "work_mem = '8MB'" in new
    assert not new.strip().startswith("#work_mem")


def test_set_conf_value_appends_when_absent():
    content = "max_connections = 100\n"
    new, changed, _old = set_conf_value(content, "log_min_duration_statement", "500")
    assert changed
    assert "log_min_duration_statement = 500" in new


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
