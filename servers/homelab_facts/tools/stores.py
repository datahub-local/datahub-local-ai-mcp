"""`object_store_health()`, `stream_health()`, `metrics_store_health()`.

The three stores that keep bytes and speak no SQL: Garage (S3), Redpanda
(streams) and Prometheus's own TSDB. Same contract as `databases.py` - one call
per report section, every expression owned here, every absence a value.

Four readings here are wrong if assembled by hand, and each is why the
corresponding expression is a constant in this file rather than prose in a
prompt:

- **Garage's informative series carry no `garage_` prefix.** Only four metrics
  do (`garage_local_disk_*`, `garage_replication_factor`, `garage_build_info`);
  the cluster, table, block and S3 API series are bare `cluster_healthy`,
  `table_size`, `block_resync_*`, `api_s3_*`. A prompt told to look at `garage_*`
  finds four metrics and concludes the store is barely instrumented, so every
  query here is scoped by job instead.
- **Garage's three nodes report one shared data filesystem.** The `data` volumes
  are `nfs` PVCs on the same 1.9 TB share, so all three publish byte-identical
  `garage_local_disk_*`; a per-node percentage there is the share's fill
  repeated three times. Whether they share is *derived* - identical totals and
  identical available - never assumed, so the day they stop sharing the tool
  reports three rows without a change here. The `meta` volumes are 500Mi of
  longhorn and are genuinely per-node, which is also the volume that stops
  Garage when it fills.
- **Redpanda's `redpanda_cluster_*` scalars come from the controller leader
  only.** One pod of three publishes brokers, topics, partitions and
  unavailable-partition counts, and which pod moves with leadership. Read
  per-pod that is two brokers with no data; the aggregate is the only correct
  reading, and "one pod answered" is normal rather than a finding.
- **A young TSDB is not a lossy one.** Held history is compared against the
  server's own uptime as well as against the configured retention, so a
  Prometheus that has kept everything since it started reads as filling rather
  than as thirty days of missing data - a finding that would otherwise fire
  every run for a month after any restart.

See ../../README.md.
"""

from __future__ import annotations

from mcp_runner import render
from mcp_runner.budget import truncate_lines
from mcp_runner.garage import GarageError, GarageUnconfigured, bucket_name
from mcp_runner.prometheus import PrometheusError, increase_, used_percent

from .. import settings

OBJECT_STORE_BUDGET = 3584
STREAM_BUDGET = 2560
METRICS_STORE_BUDGET = 2560

# Scoped by the software's own name, not by the Helm release that deployed it.
# The bare `cluster_*`, `table_*`, `block_*` and `api_s3_*` names are Garage's
# and would collide with any other exporter publishing them, so the scope is
# load-bearing rather than tidy.
GARAGE = '{job=~".*garage.*"}'
REDPANDA = '{job=~".*redpanda.*"}'

# `job=~".*prometheus.*"` also matches this stack's Grafana, whose job is
# `...-kube-prometheus-stack-grafana`. The TSDB series are the only reliable way
# to say "the process that owns this TSDB", so uptime is filtered through one.
PROMETHEUS_SELF = "and on(instance, job) prometheus_tsdb_head_series"

# From Prometheus's metadata API on 2026-08-31, not inferred from names. Two of
# these carry no `_total` at all and one is a `_counter`, so the suffix is again
# no guide - see `databases.CUMULATIVE_COUNTERS`, which holds the same kind of
# list for the same reason.
STORE_COUNTERS = frozenset(
    {
        "api_s3_request_counter",
        "api_s3_error_counter",
        "redpanda_raft_leadership_changes",
        "redpanda_kafka_records_produced_total",
        "redpanda_kafka_records_fetched_total",
        "redpanda_rpc_request_errors_total",
        "prometheus_tsdb_wal_corruptions_total",
        "prometheus_tsdb_compactions_failed_total",
        "prometheus_tsdb_wal_writes_failed_total",
        "prometheus_tsdb_head_samples_appended_total",
    }
)

# Gauges whose name invites a rate or an increase. Every one of these is the
# state now: taking a window of them answers a question nobody asked.
STORE_GAUGES = frozenset(
    {
        "block_resync_errored_blocks",
        "block_resync_queue_length",
        "redpanda_kafka_under_replicated_replicas",
        "redpanda_cluster_unavailable_partitions",
        "redpanda_storage_disk_free_space_alert",
        "cluster_healthy",
        "cluster_available",
    }
)

# What Redpanda's free-space alert means. An enum rendered as a number is a
# number the model will compare against a threshold it invented.
_DISK_ALERT = {0: "none", 1: "LOW SPACE", 2: "DEGRADED - writes rejected"}


# -- shared helpers ----------------------------------------------------------


def _one(expr: str) -> float | None:
    """One number from an expression that yields at most one series.

    ``None`` for a failed query and for an empty result alike: both render as
    `unavailable`, which is "no value for this", never zero. Where an empty
    result genuinely *means* zero the caller writes `or vector(0)` into the
    expression, so the difference is stated in PromQL rather than guessed here.
    """
    try:
        series = settings.prometheus().instant(expr)
    except PrometheusError:
        return None
    for item in series:
        try:
            return float(item["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return None


def _fill_state(percent: float | None, warn: float, critical: float) -> str:
    if percent is None:
        return "unavailable"
    if percent >= critical:
        return "CRITICAL"
    if percent >= warn:
        return "warn"
    return "ok"


# How far two nodes' free-space readings for one share may differ and still be
# the same share. They scrape at slightly different moments, so byte equality is
# too strict: the live cluster reports a 1 MB spread on a 1.9 TB share, which is
# 0.00005% and was enough to make an exact test say "three separate disks".
_SHARED_TOLERANCE = 0.005


def _shared_filesystem(available: dict[str, float], total: dict[str, float]) -> bool:
    """Whether every reporter is describing one filesystem.

    Derived, never configured: identical capacity plus free space that agrees to
    within a scrape's worth of drift is one share seen once per node. A cluster
    that stops sharing reports per node without an edit here, and one that starts
    sharing stops repeating a figure once per node.
    """
    if len(total) < 2 or len(set(total.values())) != 1 or len(available) != len(total):
        return False
    capacity = next(iter(total.values()))
    if not capacity:
        return False
    spread = max(available.values()) - min(available.values())
    return spread / capacity <= _SHARED_TOLERANCE


# -- Garage ------------------------------------------------------------------


def _garage_disk(volume: str) -> str:
    return used_percent(
        "garage_local_disk_avail",
        "garage_local_disk_total",
        GARAGE[:-1] + f',volume="{volume}"}}',
    )


def object_store_health() -> str:
    prometheus = settings.prometheus()
    limits = settings.thresholds("object_store")

    healthy = prometheus.try_scalar_by(f"cluster_healthy{GARAGE}", "pod")
    available = prometheus.try_scalar_by(f"cluster_available{GARAGE}", "pod")
    connected = prometheus.try_scalar_by(f"cluster_connected_nodes{GARAGE}", "pod")
    known = prometheus.try_scalar_by(f"cluster_known_nodes{GARAGE}", "pod")
    storage_ok = prometheus.try_scalar_by(f"cluster_storage_nodes_ok{GARAGE}", "pod")
    storage = prometheus.try_scalar_by(f"cluster_storage_nodes{GARAGE}", "pod")
    quorum = prometheus.try_scalar_by(f"cluster_partitions_quorum{GARAGE}", "pod")
    partitions = prometheus.try_scalar_by(f"cluster_partitions{GARAGE}", "pod")
    all_ok = prometheus.try_scalar_by(f"cluster_partitions_all_ok{GARAGE}", "pod")

    if not healthy and not partitions:
        return (
            "unavailable - no Garage series answered. Note that only four of "
            "Garage's metrics carry a `garage_` prefix; the cluster, table and S3 "
            "series are bare `cluster_*`, `table_*` and `api_s3_*` names scoped by "
            "job. An empty answer here is an unknown state, not a healthy one."
        )

    lines = ["## Cluster consensus (each node's own view)"]
    rows = []
    for pod in sorted(set(healthy) | set(partitions) | set(available)):
        rows.append(
            [
                pod,
                "healthy" if (healthy.get(pod) or 0) >= 1 else "NOT HEALTHY",
                "available" if (available.get(pod) or 0) >= 1 else "NOT AVAILABLE",
                f"{render.number(connected.get(pod), 0)}/{render.number(known.get(pod), 0)}",
                f"{render.number(storage_ok.get(pod), 0)}/{render.number(storage.get(pod), 0)}",
                "/".join(
                    render.number(reading.get(pod), 0)
                    for reading in (quorum, all_ok, partitions)
                ),
            ]
        )
    lines += render.table(
        ["node", "health", "availability", "peers", "storage nodes", "quorum/ok/total"],
        rows,
    )
    lines.append(
        "Partitions are quorum / fully replicated / total: a gap between the first "
        "two is redundancy lost without an outage yet. Every node publishes its own "
        "view, so rows that disagree are the finding - one node that cannot see the "
        "others is a split cluster whatever the majority says."
    )

    factor = _one(f"max(garage_replication_factor{GARAGE})")

    lines.append("")
    lines += _garage_capacity(factor)

    lines.append("")
    lines += _garage_disks(limits)

    lines.append("")
    lines.append("## Durability")
    if factor is None:
        lines.append("unavailable - garage_replication_factor answered nothing.")
    elif factor <= 1:
        lines.append(
            "Replication factor is 1: every object exists on exactly one node, so "
            "losing one node's data volume loses the objects it held. That is a "
            "standing property of how this store is configured, worth stating once "
            "as a suggestion rather than raised as a new finding every run."
        )
    else:
        lines.append(f"Replication factor is {factor:.0f}.")

    lines.append("")
    lines += _garage_traffic(limits)
    return truncate_lines(lines, OBJECT_STORE_BUDGET, unit="lines")


def _layout_capacity() -> tuple[dict[str, float], int]:
    """Capacity the cluster layout assigns each node, and how many nodes carry one.

    `role_capacity` is a **label**, not a sample value, so this cannot be summed
    in PromQL - the parsing happens here instead. Every node publishes the whole
    layout, so the series arrive once per reporter per node id and are
    deduplicated by id.
    """
    try:
        series = settings.prometheus().instant(f"cluster_layout_node_connected{GARAGE}")
    except PrometheusError:
        return {}, 0
    capacity: dict[str, float] = {}
    for item in series:
        metric = item.get("metric") or {}
        node_id = metric.get("id")
        raw = metric.get("role_capacity")
        if not node_id or raw is None:
            continue
        try:
            capacity[node_id] = float(raw)
        except (TypeError, ValueError):
            continue
    return capacity, len(capacity)


def _garage_capacity(factor: float | None) -> list[str]:
    """How much object storage there is, which is not how big the disk is.

    Garage writes only up to the capacity its layout assigns a node. Here that
    is 10 GiB a node against a 1.8 TiB filesystem, so quoting the filesystem as
    the store's headroom overstates it by two orders of magnitude and would show
    a full store as 1% used.
    """
    capacity, nodes = _layout_capacity()
    lines = ["## Capacity"]
    if not capacity:
        lines.append(
            "unavailable - cluster_layout_node_connected answered nothing, so the "
            "assigned capacity is unknown. The disk figures below are the "
            "filesystem's and are NOT the store's headroom."
        )
    else:
        assigned = sum(capacity.values())
        lines += render.table(
            ["node id", "assigned"],
            [[node_id, render.bytes_human(capacity[node_id])] for node_id in sorted(capacity)],
        )
        usable = assigned / factor if factor else None
        lines.append(
            f"{render.bytes_human(assigned)} assigned across {nodes} node(s); at "
            f"replication factor {render.number(factor, 0)} that is "
            f"{render.bytes_human(usable)} of usable object storage."
        )
        lines.append(
            "This is the store's real headroom, not the disk below it: Garage "
            "refuses writes at a node's assigned capacity however empty its "
            "filesystem is."
        )

    objects = _one(f'sum(table_size{GARAGE[:-1]},table_name="object"}})')
    lines.append(f"{render.number(objects, 0)} object(s) stored.")
    lines.append("")
    lines += _buckets()
    return lines


def _buckets() -> list[str]:
    """Per-bucket usage, which is the one reading here that needs a credential.

    Prometheus carries no bucket label and no stored-bytes gauge, so this comes
    from Garage's admin API. With no token configured the section says so: an
    absent credential is a stated blind spot, never a bucket list of zero and
    never a total silently apportioned across buckets.
    """
    client = settings.garage()
    lines = ["## Buckets"]
    try:
        buckets = client.list_buckets()
    except GarageUnconfigured:
        lines.append(
            "unavailable - no admin token is configured, and per-bucket size and "
            "object count exist nowhere else: Prometheus publishes no bucket label "
            "and no stored-bytes gauge. Report the totals above as totals. Do not "
            "split them across buckets, and do not infer how many buckets there are."
        )
        return lines
    except GarageError as exc:
        lines.append(f"ERROR: the Garage admin API could not be read: {exc}")
        return lines

    if not buckets:
        lines.append("No buckets exist. An empty list, read successfully - not an unknown.")
        return lines

    rows = []
    failures = 0
    for bucket in buckets:
        identifier = bucket.get("id")
        try:
            info = client.bucket_info(identifier) if identifier else {}
        except GarageError:
            failures += 1
            info = {}
        quota = ((info.get("quotas") or {}).get("maxSize")) if info else None
        rows.append(
            [
                bucket_name(bucket),
                render.bytes_human(info.get("bytes")),
                render.number(info.get("objects"), 0),
                render.bytes_human(quota) if quota else "none",
            ]
        )
    rows.sort(key=lambda row: row[0])
    lines += render.table(["bucket", "size", "objects", "quota"], rows)
    if failures:
        lines.append(
            f"{failures} bucket(s) could not be read and show `unavailable` rather "
            f"than zero."
        )
    lines.append(
        "Sizes are Garage's own accounting per bucket. A bucket with no quota is "
        "bounded only by the assigned capacity above, which every bucket shares."
    )
    return lines


def _garage_disks(limits: dict) -> list[str]:
    """The filesystems under the store. Bytes as well as percent, because the
    percentages here are of disks far larger than the capacity Garage will use."""
    prometheus = settings.prometheus()
    lines: list[str] = []

    for volume, warn_key, critical_key, note in (
        (
            "metadata",
            "metadata_warn_percent",
            "metadata_critical_percent",
            (
                "The small volume, and the one that stops the store: Garage cannot "
                "write an object whose metadata it cannot record."
            ),
        ),
        (
            "data",
            "data_warn_percent",
            "data_critical_percent",
            (
                "The filesystem behind the objects. Weigh its free space against "
                "the assigned capacity above: a disk with room to spare cannot "
                "help a node that has reached its layout capacity."
            ),
        ),
    ):
        warn = float(limits.get(warn_key, 70))
        critical = float(limits.get(critical_key, 85))
        used = prometheus.try_scalar_by(_garage_disk(volume), "pod")
        available = prometheus.try_scalar_by(
            f'garage_local_disk_avail{GARAGE[:-1]},volume="{volume}"}}', "pod"
        )
        total = prometheus.try_scalar_by(
            f'garage_local_disk_total{GARAGE[:-1]},volume="{volume}"}}', "pod"
        )

        if lines:
            lines.append("")
        lines.append(f"## {volume.capitalize()} filesystem")
        if not used:
            lines.append(
                f"unavailable - garage_local_disk_* answered nothing for the "
                f"{volume} volume."
            )
            continue

        if _shared_filesystem(available, total):
            percent = next(iter(used.values()))
            lines.append(
                f"{render.bytes_human(next(iter(total.values())) - next(iter(available.values())))} "
                f"used, {render.bytes_human(next(iter(available.values())))} free of "
                f"{render.bytes_human(next(iter(total.values())))} - "
                f"{render.number(percent, 1, '%')}, "
                f"{_fill_state(percent, warn, critical)} "
                f"(warn>={warn:.0f}% critical>={critical:.0f}%)."
            )
            lines.append(
                f"All {len(total)} nodes report the same capacity and agree on the "
                f"free space, so this is one shared filesystem seen {len(total)} "
                f"times and is reported once: a row per node would be the same "
                f"share counted once per node, and no node can fill it alone."
            )
        else:
            lines += render.table(
                ["node", "used", "free", "capacity", "state"],
                [
                    [
                        pod,
                        render.number(used[pod], 1, "%"),
                        render.bytes_human(available.get(pod)),
                        render.bytes_human(total.get(pod)),
                        _fill_state(used[pod], warn, critical),
                    ]
                    for pod in sorted(used, key=lambda key: -used[key])
                ],
            )
            lines.append(f"warn>={warn:.0f}% critical>={critical:.0f}%. {note}")
            continue
        lines.append(note)

    return lines


def _garage_traffic(limits: dict) -> list[str]:
    prometheus = settings.prometheus()
    window = "1h"
    error_warn = float(limits.get("s3_error_increase_warn", 1))
    resync_warn = float(limits.get("resync_errored_blocks_warn", 1))

    lines = [f"## S3 traffic and errors, last {window}"]
    requests = _one(f"sum({increase_(f'api_s3_request_counter{GARAGE}', window)})")
    lines.append(f"{render.number(requests, 0)} S3 request(s) in the last {window}.")

    errors = prometheus.try_scalar_by(
        f"sum by (status_code) ({increase_(f'api_s3_error_counter{GARAGE}', window)})",
        "status_code",
    )
    firing = {code: value for code, value in errors.items() if value >= error_warn}
    if firing:
        lines += render.table(
            ["status", f"errors/{window}"],
            [[code, render.number(firing[code], 0)] for code in sorted(firing)],
        )
    else:
        lines.append(
            f"No S3 errors in the last {window}. These are counters: a non-zero "
            "lifetime total with a zero increase is not a finding."
        )

    errored = _one(f"sum(block_resync_errored_blocks{GARAGE})")
    queued = _one(f"sum(block_resync_queue_length{GARAGE})")
    lines.append(
        f"Block resync: {render.number(errored, 0)} errored, "
        f"{render.number(queued, 0)} queued. Both are gauges, so the value is the "
        "state now - do not take a rate of them."
    )
    if errored is not None and errored >= resync_warn:
        lines.append(
            "Errored blocks are blocks Garage could not re-replicate, which is data "
            "at risk rather than a slow queue."
        )
    return lines


# -- Redpanda ----------------------------------------------------------------


def stream_health() -> str:
    limits = settings.thresholds("streams")
    window = "1h"

    brokers = _one(f"max(redpanda_cluster_brokers{REDPANDA})")
    topics = _one(f"max(redpanda_cluster_topics{REDPANDA})")
    partitions = _one(f"max(redpanda_cluster_partitions{REDPANDA})")
    unavailable = _one(f"max(redpanda_cluster_unavailable_partitions{REDPANDA})")
    under_replicated = _one(f"sum(redpanda_kafka_under_replicated_replicas{REDPANDA})")

    disk = settings.prometheus().try_scalar_by(
        used_percent("redpanda_storage_disk_free_bytes", "redpanda_storage_disk_total_bytes", REDPANDA),
        "pod",
    )
    if brokers is None and not disk:
        return (
            "unavailable - no redpanda_* series answered. That is an unknown state, "
            "not a healthy one."
        )

    unavailable_warn = float(limits.get("unavailable_partitions_warn", 1))
    replica_warn = float(limits.get("under_replicated_warn", 1))

    lines = ["## Cluster"]
    lines += render.table(
        ["brokers", "topics", "partitions", "unavailable", "under-replicated"],
        [
            [
                render.number(brokers, 0),
                render.number(topics, 0),
                render.number(partitions, 0),
                render.number(unavailable, 0),
                render.number(under_replicated, 0),
            ]
        ],
    )
    lines.append(
        "These are cluster-wide figures published by the controller leader alone, "
        "so exactly one broker carries them and which one moves with leadership. "
        "One reporter out of three is the normal state here, not two brokers "
        "missing."
    )
    if unavailable is not None and unavailable >= unavailable_warn:
        lines.append(
            f"{render.number(unavailable, 0)} partition(s) unavailable: a producer "
            "or consumer of those partitions is failing right now."
        )
    if under_replicated is not None and under_replicated >= replica_warn:
        lines.append(
            f"{render.number(under_replicated, 0)} under-replicated replica(s): the "
            "cluster is serving but has lost redundancy."
        )
    if not (unavailable or under_replicated):
        lines.append("No unavailable or under-replicated partitions. Both are gauges.")

    lines.append("")
    lines += _redpanda_disks(disk, limits)
    lines.append("")
    lines += _redpanda_traffic(limits, window)
    return truncate_lines(lines, STREAM_BUDGET, unit="lines")


def _redpanda_disks(disk: dict[str, float], limits: dict) -> list[str]:
    warn = float(limits.get("disk_warn_percent", 70))
    critical = float(limits.get("disk_critical_percent", 85))
    alert = settings.prometheus().try_scalar_by(
        f"redpanda_storage_disk_free_space_alert{REDPANDA}", "pod"
    )

    lines = ["## Broker log disks, percent USED"]
    if not disk:
        lines.append("unavailable - redpanda_storage_disk_* answered nothing.")
        return lines
    lines += render.table(
        ["broker", "used", "state", "redpanda's own alert"],
        [
            [
                pod,
                render.number(disk[pod], 1, "%"),
                _fill_state(disk[pod], warn, critical),
                _DISK_ALERT.get(int(alert[pod]), f"unknown code {alert[pod]:.0f}")
                if pod in alert
                else "unavailable",
            ]
            for pod in sorted(disk, key=lambda key: -disk[key])
        ],
    )
    lines.append(
        f"warn>={warn:.0f}% critical>={critical:.0f}%. The last column is Redpanda's "
        "own three-state judgement, not a repeat of the percentage: `none` is fine, "
        "`LOW SPACE` throttles, `DEGRADED` means it has already stopped accepting "
        "writes. Trust it over the percentage where they disagree - it knows its "
        "own reserved space."
    )
    return lines


def _redpanda_traffic(limits: dict, window: str) -> list[str]:
    produced = _one(f"sum({increase_(f'redpanda_kafka_records_produced_total{REDPANDA}', window)})")
    fetched = _one(f"sum({increase_(f'redpanda_kafka_records_fetched_total{REDPANDA}', window)})")
    leadership = _one(f"sum({increase_(f'redpanda_raft_leadership_changes{REDPANDA}', window)})")
    rpc_errors = _one(f"sum({increase_(f'redpanda_rpc_request_errors_total{REDPANDA}', window)})")

    churn_warn = float(limits.get("leadership_change_increase_warn", 10))
    error_warn = float(limits.get("rpc_error_increase_warn", 1))

    lines = [f"## Throughput and churn, last {window}"]
    lines += render.table(
        ["records produced", "records fetched", "leadership changes", "rpc errors"],
        [
            [
                render.number(produced, 0),
                render.number(fetched, 0),
                render.number(leadership, 0),
                render.number(rpc_errors, 0),
            ]
        ],
    )
    lines.append(
        f"All four are increases over the last {window}, taken from counters. A "
        "lifetime total is not a state and does not appear here. Note that "
        "redpanda_raft_leadership_changes is a counter despite carrying no _total "
        "suffix."
    )
    if leadership is not None and leadership >= churn_warn:
        lines.append(
            f"Leadership changed {render.number(leadership, 0)} time(s) in {window} "
            f"(warn>={churn_warn:.0f}): sustained churn means brokers are losing "
            "contact with each other, not that traffic is high."
        )
    if rpc_errors is not None and rpc_errors >= error_warn:
        lines.append(
            f"{render.number(rpc_errors, 0)} inter-broker RPC error(s) in {window} "
            f"(warn>={error_warn:.0f})."
        )
    if produced == 0 and fetched == 0:
        lines.append(
            f"Nothing was produced or fetched in the last {window}. That is an idle "
            "cluster, which may be correct here - it is not by itself a fault."
        )
    return lines


# -- Prometheus's own TSDB ---------------------------------------------------


def metrics_store_health() -> str:
    limits = settings.thresholds("metrics_store")

    configured = _one("max(prometheus_tsdb_retention_limit_seconds)")
    held = _one("max(time() - prometheus_tsdb_lowest_timestamp_seconds)")
    uptime = _one(f"max(time() - process_start_time_seconds {PROMETHEUS_SELF})")
    if configured is None and held is None:
        return (
            "unavailable - no prometheus_tsdb_* series answered. Every reading in "
            "this fleet is served by this store, so an unknown state here makes "
            "every other reading unverified rather than healthy."
        )

    lines = ["## Retention"]
    lines.append(
        f"Configured {render.number((configured or 0) / 86400, 1, 'd')} of history; "
        f"holding {render.number(held / 86400 if held is not None else None, 2, 'd')}. "
        f"Server up {render.number(uptime / 86400 if uptime is not None else None, 2, 'd')}."
    )
    lines.append(_retention_verdict(configured, held, uptime, limits))

    lines.append("")
    lines.append("## Size on disk")
    size = _one(
        "max(prometheus_tsdb_storage_blocks_bytes + prometheus_tsdb_head_chunks_storage_size_bytes"
        " + prometheus_tsdb_wal_storage_size_bytes)"
    )
    size_limit = _one("max(prometheus_tsdb_retention_limit_bytes)")
    warn = float(limits.get("size_warn_percent", 70))
    critical = float(limits.get("size_critical_percent", 85))
    percent = 100 * size / size_limit if size is not None and size_limit else None
    free = size_limit - size if size is not None and size_limit is not None else None
    lines.append(
        f"{render.bytes_human(size)} used, {render.bytes_human(free)} left of a "
        f"{render.bytes_human(size_limit)} limit - {render.number(percent, 1, '%')}, "
        f"{_fill_state(percent, warn, critical)} "
        f"(warn>={warn:.0f}% critical>={critical:.0f}%)."
    )
    lines.append(
        "Blocks plus head chunks plus write-ahead log, which is what the TSDB "
        "occupies. Reaching the limit costs history, not writes: Prometheus drops "
        "the oldest blocks and keeps ingesting."
    )

    lines.append("")
    lines.append("## Rows")
    series = _one("max(prometheus_tsdb_head_series)")
    chunks = _one("max(prometheus_tsdb_head_chunks)")
    appended = _one("sum(rate(prometheus_tsdb_head_samples_appended_total[5m]))")
    per_day = appended * 86400 if appended is not None else None
    lines += render.table(
        ["active series", "head chunks", "samples/s", "samples/day", "bytes/series"],
        [
            [
                render.number(series, 0),
                render.number(chunks, 0),
                render.number(appended, 0),
                render.number(per_day, 0),
                render.bytes_human(size / series if size is not None and series else None),
            ]
        ],
    )
    lines.append(
        "samples/day is the current 5m ingest rate extended over a day, not a "
        "count of what is stored - no metric here counts stored samples, so do not "
        "report one. bytes/series is the whole store divided by its live series."
    )
    lines.append(_projection(size, size_limit, held, configured))

    lines.append("")
    lines += _tsdb_integrity()

    lines.append("")
    lines += _scrape_coverage(limits)
    return truncate_lines(lines, METRICS_STORE_BUDGET, unit="lines")


def _retention_verdict(
    configured: float | None, held: float | None, uptime: float | None, limits: dict
) -> str:
    """Whether short history is loss or youth. The distinction is the whole tool.

    Held history below the configured window is the normal state of a store that
    restarted recently and would otherwise be a CRITICAL on every run for a
    month. It is only a finding when the store has been up long enough to have
    kept more than it is keeping.
    """
    if configured is None or held is None:
        return "unavailable - retention cannot be compared."
    ratio_warn = float(limits.get("retention_held_warn_ratio", 0.9))
    if held >= configured * ratio_warn:
        return "At its configured depth."
    if uptime is not None and held >= uptime * 0.95:
        return (
            f"Filling, not losing: the store holds essentially everything since it "
            f"started {render.number(uptime / 3600, 1, 'h')} ago. Nothing has been "
            f"dropped - but every trend anyone can look at is limited to that "
            f"window until it grows."
        )
    return (
        f"WARN: holding {render.number(held / 86400, 2, 'd')} against a configured "
        f"{render.number(configured / 86400, 1, 'd')} while up for "
        f"{render.number(uptime / 86400 if uptime is not None else None, 2, 'd')}. "
        f"History is being dropped earlier than configured - usually the size limit "
        f"biting before the time limit."
    )


def _projection(
    size: float | None,
    size_limit: float | None,
    held: float | None,
    configured: float | None,
) -> str:
    """Which of the two retention limits arrives first, at the present growth.

    Prometheus keeps data until whichever of the time limit and the size limit
    binds, and only the time limit is ever configured deliberately. The estimate
    divides the store by the history behind it, so it needs enough history to
    divide by: under half a day it says so instead of extrapolating a number
    from minutes.
    """
    if not size or not size_limit or not held or not configured:
        return "Growth cannot be projected: a size or retention reading is unavailable."
    if held < 43200:
        return (
            f"Growth is not projected from "
            f"{render.number(held / 3600, 1, 'h')} of history - too little to divide "
            f"by. It becomes a usable estimate once half a day has accumulated."
        )
    per_day = size / (held / 86400)
    days_to_limit = (size_limit - size) / per_day if per_day else None
    configured_days = configured / 86400
    projected = per_day * configured_days
    verdict = (
        f"the size limit binds first, at roughly "
        f"{render.number(size_limit / per_day, 0, 'd')} of history"
        if projected > size_limit
        else f"the {render.number(configured_days, 0, 'd')} time limit binds first"
    )
    return (
        f"Growing about {render.bytes_human(per_day)}/day, so a full "
        f"{render.number(configured_days, 0, 'd')} would need "
        f"{render.bytes_human(projected)} against a {render.bytes_human(size_limit)} "
        f"limit: {verdict}. Room left at this rate: "
        f"{render.number(days_to_limit, 0, 'd')}."
    )


def _tsdb_integrity() -> list[str]:
    clean = _one("min(prometheus_tsdb_clean_start)")
    corruptions = _one(f"sum({increase_('prometheus_tsdb_wal_corruptions_total', '24h')})")
    compactions = _one(f"sum({increase_('prometheus_tsdb_compactions_failed_total', '24h')})")
    writes = _one(f"sum({increase_('prometheus_tsdb_wal_writes_failed_total', '1h')})")

    lines = ["## Integrity"]
    lines += render.table(
        ["clean start", "wal corruptions/24h", "failed compactions/24h", "wal write failures/1h"],
        [
            [
                "yes" if clean is not None and clean >= 1 else ("no" if clean == 0 else "unavailable"),
                render.number(corruptions, 0),
                render.number(compactions, 0),
                render.number(writes, 0),
            ]
        ],
    )
    if clean == 0:
        lines.append(
            "The last start was not clean: the write-ahead log had to be replayed "
            "after an unclean shutdown. Samples in flight at the time are gone."
        )
    if compactions:
        lines.append(
            "Failed compactions leave the store growing without ever reclaiming "
            "space, so the size limit arrives early and takes history with it."
        )
    return lines


def _scrape_coverage(limits: dict) -> list[str]:
    total = _one("count(up)")
    # `count(up == 0)` matches nothing when nothing is down, and an empty result
    # is not a zero anywhere else in this server. `or vector(0)` is where that
    # exception is stated, in PromQL, rather than assumed by the caller.
    down = _one("count(up == 0) or vector(0)")
    warn = float(limits.get("targets_down_warn", 1))

    lines = ["## Scrape coverage"]
    lines.append(
        f"{render.number(down, 0)} of {render.number(total, 0)} scrape targets are "
        f"down (warn>={warn:.0f})."
    )
    lines.append(
        "A down target is a hole in some other reading, not a healthy zero: "
        "whatever it was scraping answers `unavailable` in every tool that asks."
    )
    return lines


# -- tool manifest -----------------------------------------------------------

OBJECT_STORE_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
STREAM_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
METRICS_STORE_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

OBJECT_STORE_DESCRIPTION = """
Garage (S3) state: per-node cluster consensus, metadata and data disk fill,
replication factor, and S3 traffic and errors over the last hour.

Takes no arguments. The data volume is one shared filesystem seen by every node,
so it is reported once rather than once per node - that is not a missing row.
Error figures are already increases over a window; there is no lifetime total
here to misread.
"""

STREAM_DESCRIPTION = """
Redpanda state: brokers, topics and partitions, unavailable and under-replicated
partitions, per-broker log disk fill, and throughput and leadership churn over
the last hour.

Takes no arguments. The cluster-wide figures are published by the controller
leader alone, so one broker of three reports them and that is normal. The disk
column carries Redpanda's own none/LOW SPACE/DEGRADED judgement alongside the
percentage.
"""

METRICS_STORE_DESCRIPTION = """
Prometheus's own storage: how much history it is configured to keep against how
much it is actually holding, size on disk, active series and ingest rate,
write-ahead log and compaction integrity, and how many scrape targets are down.

Takes no arguments. Held history shorter than the configured retention is
already judged against the server's uptime, so a recently restarted store reads
as filling rather than as lost data - report the verdict as given.
"""
