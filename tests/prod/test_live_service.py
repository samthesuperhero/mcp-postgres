"""Live service test — runs the on-deploy self-test against the running service.

Skipped automatically when the service isn't reachable (e.g. on a dev host), so
it's safe to include in a normal ``pytest`` run and meaningful post-deploy.
"""

import pytest

from mcp_postgres.config import load_config
from mcp_postgres.selftest import run_all


def test_selftest_passes_when_service_running():
    cfg = load_config()
    res = run_all(cfg)
    # ``checks`` are (name, ok, detail, warn); advisory (warn) checks never fail.
    live = {name: (ok, detail, warn) for name, ok, detail, warn in res.checks}

    # If we couldn't reach the live endpoint, this is a dev host — skip.
    if "mcp-live" in live and not live["mcp-live"][0]:
        pytest.skip(f"service not reachable: {live['mcp-live'][1]}")

    failures = [f"{n}: {d}" for n, (ok, d, warn) in live.items() if not ok and not warn]
    assert not failures, "self-test failures: " + "; ".join(failures)
