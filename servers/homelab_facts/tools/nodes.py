"""`node_fleet()` - the node table, with machine identity joined in the query.

No `node_*` series here carries a machine name; they are keyed by `instance`, and
the only hostname anywhere is `nodename` on `node_uname_info`. Every expression
below carries that join, built by `by_nodename` so it cannot be dropped.

Three rules are enforced rather than asked for: nothing about the homelab is
written down (see `mcp_runner.fleet`), `unavailable` and `n/a` mean different
things, and versions compare within a hardware class only. See ../../README.md.
"""

from __future__ import annotations

import logging

from mcp_runner import fleet, render
from mcp_runner.budget import truncate_lines
from mcp_runner.prometheus import (
    UNAVAILABLE,
    Reading,
    by_nodename,
    increase_,
    rate_percent,
    used_percent,
)

from .. import settings

logger = logging.getLogger(__name__)

BUDGET = 3584
_NOT_APPLICABLE = "n/a"

# Every reading, with the join already applied. Each verified present in this
# Prometheus.
_QUERIES: dict[str, str] = {
    "disk_pct": by_nodename(
        used_percent(
            "node_filesystem_avail_bytes",
            "node_filesystem_size_bytes",
            '{fstype!~"tmpfs|overlay|ramfs"}',
        )
    ),
    "io_stall_pct": by_nodename(rate_percent("node_pressure_io_stalled_seconds_total")),
    "cpu_stall_pct": by_nodename(rate_percent("node_pressure_cpu_waiting_seconds_total")),
    "mem_stall_pct": by_nodename(rate_percent("node_pressure_memory_stalled_seconds_total")),
    "io_busy_pct": by_nodename(rate_percent("node_disk_io_time_seconds_total")),
    # node_hwmon_temp_celsius covers every node; smartmon_temperature_celcius
    # only those with reporting drives, so they are separate columns.
    "temp_c": by_nodename("node_hwmon_temp_celsius"),
    "temp_crit": by_nodename("node_hwmon_temp_crit_alarm_celsius"),
    "uptime_d": by_nodename("(time() - node_boot_time_seconds) / 86400"),
    "apt_security": by_nodename("node_apt_security_upgrades_pending"),
    "apt_total": by_nodename("node_apt_upgrades_pending"),
    "reboot_required": by_nodename("node_reboot_required"),
    "apt_cache_age_d": by_nodename("(time() - node_apt_package_cache_timestamp_seconds) / 86400"),
    "systemd_ok": by_nodename("node_systemd_system_running"),
}

# Readings that exist only where the hardware does, each paired with the probe
# that decides per node whether the sensor is there - so a gap is `n/a` rather
# than a finding, with no list of which machine has what.
_SCOPED: dict[str, tuple[str, str, str]] = {
    # (expression, capability probe, aggregator)
    "smart_healthy": (
        by_nodename("smartmon_device_smart_healthy", "min"),
        # 0 means the device cannot report health at all, so such a node gets
        # no health verdict.
        by_nodename("smartmon_device_smart_available", "max"),
        "min",
    ),
    "smart_temp_c": (
        by_nodename("smartmon_temperature_celcius"),
        by_nodename("smartmon_device_smart_available", "max"),
        "max",
    ),
    "edac_corr": (
        by_nodename(increase_("node_edac_correctable_errors_total", "24h")),
        by_nodename("node_edac_correctable_errors_total"),
        "max",
    ),
    "edac_uncorr": (
        by_nodename(increase_("node_edac_uncorrectable_errors_total", "24h")),
        by_nodename("node_edac_uncorrectable_errors_total"),
        "max",
    ),
}

_UPS_QUERIES = {
    "charge": by_nodename("network_ups_tools_battery_charge"),
    "runtime_s": by_nodename("network_ups_tools_battery_runtime"),
    "input_v": by_nodename("network_ups_tools_input_voltage"),
}


def node_fleet() -> str:
    prometheus = settings.prometheus()
    limits = settings.thresholds("nodes")

    readings: dict[str, Reading] = {
        key: prometheus.reading(expression, "nodename") for key, expression in _QUERIES.items()
    }

    scoped: dict[str, tuple[Reading, set[str]]] = {}
    for key, (expression, probe, _aggregator) in _SCOPED.items():
        value = prometheus.reading(expression, "nodename")
        capability = prometheus.reading(probe, "nodename")
        # Capable = the probe answered non-zero. A zero is an explicit "cannot
        # report", not a missing series.
        capable = {node for node, level in capability.values.items() if level and level > 0}
        scoped[key] = (value, capable)

    kernels, machines = _identity(prometheus)

    # Kubernetes says which machines exist, Prometheus which answered; the
    # difference is what makes a dropped join legible.
    try:
        expected = settings.kube().node_names()
    except Exception as exc:  # noqa: BLE001 - unreachable API must not lose metrics
        logger.warning("node list unavailable, expected set is empty: %s", exc)
        expected = []

    observed: set[str] = set(kernels)
    for reading in readings.values():
        observed |= reading.covered()
    nodes = sorted(set(expected) | observed)
    if not nodes:
        return (
            "ERROR: no node answered any query and the Kubernetes node list was "
            "unreadable. This is an unknown fleet, not a healthy one."
        )

    prefix = fleet.common_prefix(nodes)

    def cell(key: str, node: str, digits: int = 1, suffix: str = "") -> str:
        reading = readings[key]
        if not reading.ok:
            return UNAVAILABLE
        return render.number(reading.values.get(node), digits, suffix)

    def scoped_cell(key: str, node: str, digits: int = 1, suffix: str = "") -> str:
        reading, capable = scoped[key]
        if not reading.ok:
            return UNAVAILABLE
        if node not in capable:
            return _NOT_APPLICABLE
        return render.number(reading.values.get(node), digits, suffix)

    rows = []
    for node in nodes:
        release = kernels.get(node, "")
        rows.append(
            [
                fleet.shorten(node, prefix),
                fleet.kernel_flavour(release) if release else UNAVAILABLE,
                cell("disk_pct", node, 1, "%"),
                cell("io_stall_pct", node, 2, "%"),
                cell("cpu_stall_pct", node, 2, "%"),
                cell("temp_c", node, 1),
                scoped_cell("smart_temp_c", node, 1),
                cell("uptime_d", node, 1, "d"),
                cell("apt_security", node, 0),
                cell("apt_total", node, 0),
                fleet.kernel_version(release) or UNAVAILABLE,
            ]
        )

    lines = [
        (
            f"Node fleet, {len(nodes)} nodes. Every figure is joined to the machine "
            "name in the query; no column is borrowed from another metric."
        ),
        (
            "'unavailable' = the query gave no value for this node. 'n/a' = this node "
            "has no such sensor, which is the hardware and not a finding."
        ),
        "",
    ]
    lines += render.table(
        [
            "node",
            "class",
            "disk",
            "iostall",
            "cpustall",
            "temp",
            "smart",
            "uptime",
            "sec-upd",
            "upd",
            "kernel",
        ],
        rows,
    )

    missing_from_metrics = [node for node in expected if node not in observed]
    if missing_from_metrics:
        lines.append(
            "In the Kubernetes node list but absent from every metric: "
            + ", ".join(fleet.shorten(node, prefix) for node in missing_from_metrics)
            + ". Either node-exporter is not running there or the join is broken - "
            "scattered absences almost always mean the join, not the hardware."
        )

    lines.append("")
    lines.append("## Readings that need a note")
    lines += _notes(readings, scoped, nodes, limits, prefix)

    lines.append("")
    lines.append("## Power")
    lines += _power(prometheus, prefix)

    lines.append("")
    lines.append("## Kernel comparison, within hardware class only")
    lines += _kernel_drift(kernels, machines, prefix)

    return truncate_lines(lines, BUDGET, unit="lines")


def _identity(prometheus) -> tuple[dict[str, str], dict[str, str]]:
    """``({node: kernel release}, {node: architecture})`` from the identity metric."""
    kernels: dict[str, str] = {}
    machines: dict[str, str] = {}
    try:
        for item in prometheus.instant("node_uname_info"):
            metric = item.get("metric") or {}
            name = metric.get("nodename")
            if name:
                kernels[name] = metric.get("release", "")
                machines[name] = metric.get("machine", "")
    except Exception as exc:  # noqa: BLE001 - any client error means no identity
        # Logged, not silent: without identity every row loses class and kernel.
        logger.warning("node identity query failed, classes unavailable: %s", exc)
    return kernels, machines


def _notes(readings, scoped, nodes, limits, prefix) -> list[str]:
    """Only readings outside a threshold, or missing where they should not be."""
    notes: list[str] = []

    disk_crit = float(limits.get("disk_critical_percent", 85))
    disk_warn = float(limits.get("disk_warn_percent", 70))
    io_warn = float(limits.get("io_stall_warn_percent", 10))
    cpu_warn = float(limits.get("cpu_stall_warn_percent", 40))
    temp_warn = float(limits.get("temperature_warn_celsius", 75))
    temp_critical = float(limits.get("temperature_critical_celsius", 85))
    cache_stale = float(limits.get("apt_cache_stale_days", 7))
    security_updates_warn = float(limits.get("security_updates_warn", 1))

    def value(key: str, node: str) -> float | None:
        reading = readings[key]
        return reading.values.get(node) if reading.ok else None

    for node in nodes:
        short = fleet.shorten(node, prefix)

        disk = value("disk_pct", node)
        if disk is not None and disk >= disk_warn:
            level = "CRITICAL" if disk >= disk_crit else "warn"
            notes.append(f"{short}: disk {disk:.1f}% used ({level}, threshold {disk_warn:.0f}%).")
        io = value("io_stall_pct", node)
        if io is not None and io >= io_warn:
            notes.append(
                f"{short}: IO stall {io:.2f}% of the last 5m (threshold {io_warn:.0f}%). "
                "Sustained IO stall is the classic overloaded-or-dying-disk symptom."
            )
        cpu = value("cpu_stall_pct", node)
        if cpu is not None and cpu >= cpu_warn:
            notes.append(
                f"{short}: CPU stall {cpu:.2f}% of the last 5m (threshold {cpu_warn:.0f}%)."
            )
        temp = value("temp_c", node)
        if temp is not None and temp >= temp_warn:
            level = "CRITICAL" if temp >= temp_critical else "warn"
            threshold = temp_critical if level == "CRITICAL" else temp_warn
            notes.append(f"{short}: {temp:.1f}C ({level}, threshold {threshold:.0f}C).")
        if value("temp_crit", node):
            notes.append(f"{short}: hwmon critical temperature alarm is SET.")
        if value("reboot_required", node):
            notes.append(
                f"{short}: reboot required - a patch is installed but not running, "
                "which is the worst of both states."
            )
        security_updates = value("apt_security", node)
        if security_updates is not None and security_updates >= security_updates_warn:
            notes.append(
                f"{short}: {security_updates:.0f} security update(s) pending "
                f"(threshold {security_updates_warn:.0f})."
            )
        total_updates = value("apt_total", node)
        if total_updates is not None and total_updates > 0:
            notes.append(f"{short}: {total_updates:.0f} total package update(s) pending.")
        cache_age = value("apt_cache_age_d", node)
        if cache_age is not None and cache_age >= cache_stale:
            notes.append(
                f"{short}: apt cache is {cache_age:.0f}d old, so its update counts are "
                "stale - a 0 there is meaningless rather than good."
            )
        running = value("systemd_ok", node)
        if running is not None and running < 1:
            notes.append(f"{short}: systemd reports the system degraded.")

        healthy, smart_capable = scoped["smart_healthy"]
        if healthy.ok and node in smart_capable:
            level = healthy.values.get(node)
            if level is not None and level < 1:
                notes.append(f"{short}: SMART reports a drive FAILING.")
        uncorr, edac_capable = scoped["edac_uncorr"]
        if uncorr.ok and node in edac_capable and uncorr.values.get(node):
            notes.append(
                f"{short}: {uncorr.values[node]:.0f} uncorrectable memory error(s) in "
                "24h - serious immediately."
            )
        corr, corr_capable = scoped["edac_corr"]
        if corr.ok and node in corr_capable and corr.values.get(node):
            notes.append(
                f"{short}: {corr.values[node]:.0f} correctable memory error(s) in 24h - "
                "failing RAM announcing itself early."
            )

    broken = sorted(key for key, reading in readings.items() if not reading.ok)
    if broken:
        notes.append(
            "Queries that FAILED, so those columns are unknown fleet-wide: "
            + ", ".join(broken)
            + ". A fleet check with no metrics is a failed check, not a clean one."
        )

    return notes or ["Nothing outside a threshold."]


def _power(prometheus, prefix: str) -> list[str]:
    """UPS state, on whichever node has one. No UPS anywhere is not a finding."""
    readings = {
        key: prometheus.reading(expression, "nodename")
        for key, expression in _UPS_QUERIES.items()
    }
    if any(not reading.ok for reading in readings.values()):
        return ["unavailable - a UPS query failed, so power state is unknown."]

    hosts = sorted(set().union(*(reading.covered() for reading in readings.values())))
    if not hosts:
        return [
            (
                "No UPS is reported anywhere in this fleet. network_ups_tools_* has no "
                "series, so no monitored UPS is present - not that one is failing."
            )
        ]

    lines = []
    for host in hosts:
        charge = readings["charge"].values.get(host)
        runtime = readings["runtime_s"].values.get(host)
        volts = readings["input_v"].values.get(host)
        state = "on mains" if volts and volts > 0 else "ON BATTERY or input lost"
        lines.append(
            f"{fleet.shorten(host, prefix)}: charge {render.number(charge, 0, '%')}, "
            f"runtime {render.number(runtime / 60 if runtime is not None else None, 0, 'm')}, "
            f"input {render.number(volts, 1, 'V')} - {state}."
        )
    lines.append(
        "A UPS on battery, or a runtime estimate that has collapsed, matters more "
        "than anything else in this report."
    )
    return lines


def _kernel_drift(kernels: dict[str, str], machines: dict[str, str], prefix: str) -> list[str]:
    """Report a node trailing its own class. A class of one is never a finding."""
    if not kernels:
        return [f"Kernel versions {UNAVAILABLE} - node_uname_info answered nothing."]

    # The scope note leads this section rather than closing it. It used to trail the
    # per-class lines, and a 4B model read its "can never be actioned" clause - which
    # is about a difference *across* classes - as the verdict on the DRIFT line right
    # above it, dismissing the one real finding in the fleet as unactionable. The
    # phrase is gone and every line now carries its own verdict.
    lines: list[str] = [
        (
            "A class is one kernel tree on one architecture, derived from the running "
            "kernels rather than configured. Only nodes inside one class are compared "
            "below. Nodes in different classes run different silicon, can never "
            "converge, and are not compared here at all."
        )
    ]
    for group, members, behind in fleet.kernel_drift(kernels, machines):
        names = ", ".join(fleet.shorten(node, prefix) for node in members)
        if len(members) == 1:
            lines.append(
                f"{group}: one node ({names}) on {fleet.kernel_version(kernels[members[0]])} - "
                "a class of one has nothing to compare and is never drift."
            )
        elif not behind:
            lines.append(
                f"{group}: all {len(members)} nodes ({names}) on "
                f"{fleet.kernel_version(kernels[members[0]])} - no drift."
            )
        else:
            detail = ", ".join(
                f"{fleet.shorten(node, prefix)}={fleet.kernel_version(kernels[node])}"
                for node in sorted(members)
            )
            newest = max((kernels[node] for node in members), key=fleet.version_key)
            gaps = []
            for node in behind:
                gap = fleet.version_gap(kernels[node], newest)
                if gap:
                    gaps.append(f"{fleet.shorten(node, prefix)} is {gap}")
            lines.append(
                f"{group}: DRIFT within class - "
                f"{', '.join(fleet.shorten(node, prefix) for node in behind)} behind. {detail}. "
                + (f"{'; '.join(gaps)}. " if gaps else "")
                + "One kernel tree, one architecture, so these can converge: this is a "
                "real finding and belongs in your report. "
                "The distance is stated above and is the only quantity available: a "
                "kernel version carries no release date, so how long a node has been "
                "behind is not knowable here. Do not restate the gap in any other unit."
            )
    return lines


SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

DESCRIPTION = """
The whole node hardware table in one call: disk fill, stall percentages,
temperature, SMART, uptime, pending updates, kernel and UPS state, per machine.

Takes no arguments. Every figure is already joined to the machine name and comes
from the metric named for it. 'unavailable' means the query gave no value;
'n/a' means the node has no such sensor and is not a finding. Kernel drift is
already computed within hardware class - report only what it names.
"""
