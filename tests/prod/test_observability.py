"""Offline tests for the backend-signalling helper behind cancel_query/terminate_backend.

``_signal_backend`` refuses to signal the service's own backend and otherwise reports
whether PostgreSQL signalled the target. A fake db drives the self-check / result logic
without a live cluster (the live cancel/terminate paths are exercised in
``test_live_observability``).
"""

from types import SimpleNamespace

from mcp_postgres.tools.observability import _signal_backend


class _FakeDb:
    """Models the single self-check + signal statement _signal_backend issues.

    ``is_self`` is true when the target pid equals our own; ``signalled`` is NULL for
    self (the CASE short-circuits) and otherwise the boolean the signal function returns.
    """

    def __init__(self, own_pid: int, result: bool = True):
        self.own_pid = own_pid
        self.result = result

    def query_one(self, sql, params=None):
        pid = params[0]
        is_self = pid == self.own_pid
        return {"is_self": is_self, "signalled": None if is_self else self.result}


def _target(own_pid, result=True):
    return SimpleNamespace(db=_FakeDb(own_pid, result), dbname="db")


def test_signal_backend_refuses_own_backend():
    res = _signal_backend(_target(own_pid=42), [], 42, "pg_cancel_backend")
    assert res["ok"] is False
    assert res["pid"] == 42
    assert "own backend" in res["error"]


def test_signal_backend_reports_success():
    res = _signal_backend(_target(own_pid=42, result=True), [], 99, "pg_terminate_backend")
    assert res["ok"] is True
    assert res["pid"] == 99
    assert res["signalled"] is True


def test_signal_backend_reports_pid_already_gone():
    # pg_cancel_backend returns false when the pid no longer exists — not an error.
    res = _signal_backend(_target(own_pid=42, result=False), [], 99, "pg_cancel_backend")
    assert res["ok"] is True
    assert res["signalled"] is False


def test_signal_backend_stamps_database_and_notices():
    res = _signal_backend(_target(own_pid=1), ["DB tier changed"], 2, "pg_cancel_backend")
    assert res["database"] == "db"
    assert res["capability_changed"] == ["DB tier changed"]
