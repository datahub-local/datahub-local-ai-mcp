"""Config built in code, so these tests need no mounted ConfigMap.

The chronic set and the thresholds used to be read from a `config/` directory
inside the server package, which meant the tests silently asserted against one
specific homelab's data: `test_a_chronic_alert_is_classified_not_dropped` passed
because `Watchdog` happened to be in that cluster's file. Once the data became a
mount, those three tests failed - correctly, because the *coupling* was the bug.

The classification rules are what is under test here, not any deployment's
answer to them, so the fixture supplies a minimal set of its own and is
autouse: a test that forgets it would fall back to reading a real mount and pass
or fail on whatever that cluster believes today.
"""

from __future__ import annotations

import pytest
import yaml

# Deliberately small, and each entry earns its place:
#   Watchdog                  chronic - the always-firing alert every cluster has
#   KubeSchedulerDown         chronic - a k3s artifact, the canonical false alarm
#   CPUThrottlingHigh         chronic - fires on many pods at once, so it is the
#                             one that exercises per-alert compression: only a
#                             chronic alert reaches `summarise()`, which is what
#                             collapses 40 instances into `x40` and keeps the
#                             answer inside its byte budget
#   NodeClockNotSynchronising never_suppress - fires forever AND is really broken
CHRONIC_ALERTS = {
    "chronic": [
        {"name": "Watchdog", "reason": "An always-firing alert that proves alerting works."},
        {"name": "KubeSchedulerDown", "reason": "k3s embeds the scheduler with no metrics endpoint."},
        {"name": "CPUThrottlingHigh", "reason": "Fires broadly on tuned-down CPU limits."},
    ],
    "never_suppress": [
        {
            "name": "NodeClockNotSynchronising",
            "reason": "Fires permanently and is a real fault; must never be folded into chronic.",
        },
    ],
}

THRESHOLDS = {
    "volumes": {
        "warn_percent": 70,
        "critical_percent": 80,
        "storage_classes": ["longhorn", "longhorn-no-replica"],
    },
    "nodes": {"disk_warn_percent": 70, "disk_critical_percent": 85},
}


@pytest.fixture(autouse=True)
def mcp_config_dir(tmp_path, monkeypatch):
    """Point `MCP_CONFIG_DIR` at a directory holding both config files.

    `settings.load` is `functools.cache`d, so the cache is cleared on the way in
    *and* on the way out - a value cached under one test's tmp_path would
    otherwise leak into the next test and into the rest of the suite.
    """
    from homelab_facts import settings

    (tmp_path / "chronic_alerts.yaml").write_text(yaml.safe_dump(CHRONIC_ALERTS))
    (tmp_path / "thresholds.yaml").write_text(yaml.safe_dump(THRESHOLDS))

    monkeypatch.setenv("MCP_CONFIG_DIR", str(tmp_path))
    settings.load.cache_clear()
    yield tmp_path
    settings.load.cache_clear()
