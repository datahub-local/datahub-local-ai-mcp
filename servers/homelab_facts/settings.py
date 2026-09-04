"""Loading of this server's mounted config, and the shared client handles.

The two YAML files are **deployment data, not code**, so they are mounted rather
than shipped: `chronic_alerts.yaml` names which alerts are noise in one specific
cluster and `thresholds.yaml` sets where that cluster draws a finding. Another
cluster has a different answer to both, and neither is derivable from the code,
so baking them into the image would make the image cluster-specific - the one
property this repository exists to remove. `deploy/examples/homelab_facts/`
carries a documented copy of each; the live pair is a ConfigMap.

Absence is loud but never fatal. A missing mount costs classification and the
thresholds, which every tool then states as unset - the server still starts,
still answers `/healthz`, and still returns all sixteen readings. Fatal would
trade sixteen working tools for two missing files.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Any

import yaml

from mcp_runner import config as runner_config
from mcp_runner.garage import Garage
from mcp_runner.kube import Kube
from mcp_runner.loki import Loki
from mcp_runner.prometheus import Prometheus
from mcp_runner.state import Snapshots

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path("/etc/mcp/homelab_facts")


def config_dir() -> Path:
    """The ConfigMap mount point.

    Outside the code tree deliberately: `/app` is WORKDIR and lands on
    `sys.path`, so a mount under it can shadow a top-level package and kill the
    server at import. `/etc/mcp/<server>/` cannot collide with anything.
    """
    override = runner_config.config_dir()
    return Path(override) if override else DEFAULT_CONFIG_DIR


@functools.cache
def load(name: str) -> dict[str, Any]:
    """Load and cache one config file. A missing file is an empty mapping.

    Empty rather than fatal, but logged at WARNING with the path that was tried:
    an unclassified snapshot is a worse report than a classified one and a true
    one either way, whereas a server that refuses to start reports nothing at
    all. The log line is the only signal that a mount is missing, so it names
    the file and the directory rather than saying "using defaults".
    """
    path = config_dir() / name
    try:
        return yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        logger.warning(
            "no %s at %s: continuing without it. Mount this server's ConfigMap at %s "
            "(or set MCP_CONFIG_DIR); see deploy/examples/homelab_facts/%s.",
            name,
            path,
            config_dir(),
            name,
        )
        return {}


def thresholds(section: str) -> dict[str, Any]:
    return load("thresholds.yaml").get(section) or {}


# Nothing here describes the fleet's shape - node names, hardware classes and
# sensor coverage are all derived at query time; see `mcp_runner.fleet`.


def chronic_alerts() -> dict[str, str]:
    return {
        entry["name"]: entry.get("reason", "")
        for entry in load("chronic_alerts.yaml").get("chronic") or []
        if entry.get("name")
    }


def never_suppress() -> dict[str, str]:
    return {
        entry["name"]: entry.get("reason", "")
        for entry in load("chronic_alerts.yaml").get("never_suppress") or []
        if entry.get("name")
    }


# Client handles are lazy and cached: building a Kubernetes client costs a
# credential load, and a tool that never touches Kubernetes should not pay it.


@functools.lru_cache(maxsize=1)
def prometheus() -> Prometheus:
    return Prometheus()


@functools.lru_cache(maxsize=1)
def kube() -> Kube:
    return Kube()


@functools.lru_cache(maxsize=1)
def garage() -> Garage:
    return Garage()


@functools.lru_cache(maxsize=1)
def loki() -> Loki:
    return Loki()


@functools.lru_cache(maxsize=1)
def snapshots() -> Snapshots:
    return Snapshots()
