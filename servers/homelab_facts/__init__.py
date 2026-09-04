"""homelab_facts - deterministic readings of this homelab, one call per section.

Code gathers and the model writes. The tools are fat in calls and thin in bytes:
one call per report section, each answer bounded in code, each absence stated
rather than left for a report format to fill with an invented number.

They do not replace reach - every persona keeps the raw `k8s_*` and Prometheus
tools for following up. The win is budget reallocation. See ../../README.md.
"""

from __future__ import annotations

from mcp_runner.server import Registry

from .tools import (
    alerts,
    databases,
    diagnose,
    gitops,
    lifecycle,
    lookup,
    nodes,
    raw,
    stores,
    volumes,
)


def register(registry: Registry) -> None:
    registry.add(
        "alerts_snapshot",
        alerts.DESCRIPTION,
        alerts.alerts_snapshot,
        schema=alerts.SCHEMA,
        budget=alerts.BUDGET,
    )
    registry.add(
        "find_object",
        lookup.DESCRIPTION,
        lookup.find_object,
        schema=lookup.SCHEMA,
        budget=lookup.BUDGET,
    )
    registry.add(
        "why_failed",
        diagnose.WHY_FAILED_DESCRIPTION,
        diagnose.why_failed,
        schema=diagnose.WHY_FAILED_SCHEMA,
        budget=diagnose.BUDGET,
    )
    registry.add(
        "logs",
        diagnose.LOGS_DESCRIPTION,
        diagnose.logs,
        schema=diagnose.LOGS_SCHEMA,
        budget=diagnose.LOGS_BUDGET,
    )
    registry.add(
        "endpoints",
        diagnose.ENDPOINTS_DESCRIPTION,
        diagnose.endpoints,
        schema=diagnose.ENDPOINTS_SCHEMA,
        budget=diagnose.ENDPOINTS_BUDGET,
    )
    registry.add(
        "node_fleet",
        nodes.DESCRIPTION,
        nodes.node_fleet,
        schema=nodes.SCHEMA,
        budget=nodes.BUDGET,
    )
    registry.add(
        "volume_fill",
        volumes.DESCRIPTION,
        volumes.volume_fill,
        schema=volumes.SCHEMA,
        budget=volumes.BUDGET,
    )
    registry.add(
        "postgres_health",
        databases.POSTGRES_DESCRIPTION,
        databases.postgres_health,
        schema=databases.POSTGRES_SCHEMA,
        budget=databases.BUDGET,
    )
    registry.add(
        "cache_health",
        databases.CACHE_DESCRIPTION,
        databases.cache_health,
        schema=databases.CACHE_SCHEMA,
        budget=databases.BUDGET,
    )
    registry.add(
        "object_store_health",
        stores.OBJECT_STORE_DESCRIPTION,
        stores.object_store_health,
        schema=stores.OBJECT_STORE_SCHEMA,
        budget=stores.OBJECT_STORE_BUDGET,
    )
    registry.add(
        "stream_health",
        stores.STREAM_DESCRIPTION,
        stores.stream_health,
        schema=stores.STREAM_SCHEMA,
        budget=stores.STREAM_BUDGET,
    )
    registry.add(
        "metrics_store_health",
        stores.METRICS_STORE_DESCRIPTION,
        stores.metrics_store_health,
        schema=stores.METRICS_STORE_SCHEMA,
        budget=stores.METRICS_STORE_BUDGET,
    )
    registry.add(
        "cert_expiry",
        lifecycle.CERT_DESCRIPTION,
        lifecycle.cert_expiry,
        schema=lifecycle.CERT_SCHEMA,
        budget=lifecycle.BUDGET,
    )
    registry.add(
        "backup_freshness",
        lifecycle.BACKUP_DESCRIPTION,
        lifecycle.backup_freshness,
        schema=lifecycle.BACKUP_SCHEMA,
        budget=lifecycle.BUDGET,
    )
    registry.add(
        "argocd_drift",
        gitops.DESCRIPTION,
        gitops.argocd_drift,
        schema=gitops.SCHEMA,
        budget=gitops.BUDGET,
    )
    registry.add(
        "promql",
        raw.DESCRIPTION,
        raw.promql,
        schema=raw.SCHEMA,
        budget=raw.BUDGET,
    )
