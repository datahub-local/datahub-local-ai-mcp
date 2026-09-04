"""`cert_expiry()` and `backup_freshness()` - what is quietly expiring or stopped.

Both do arithmetic the model cannot: a raw `notAfter` date is uninterpretable
without a clock, so the server subtracts and returns days. `backup_freshness()`
summarises per schedule rather than listing objects - there are hundreds, and the
question is when each schedule last succeeded. See ../../README.md.
"""

from __future__ import annotations

from mcp_runner import render
from mcp_runner.budget import truncate_lines

from .. import settings

BUDGET = 3072

# How many recent objects to consider when counting failures.
_RECENT = 20

# Genuine failures, across Velero and CloudNativePG. `InProgress` and
# `Deleting` are neither success nor failure and are counted as neither.
_FAILED_PHASES = frozenset(
    {"failed", "partiallyfailed", "failedvalidation", "error"}
)


def cert_expiry() -> str:
    limits = settings.thresholds("lifecycle")
    horizon = float(limits.get("cert_expiry_warn_days", 21))
    kube = settings.kube()

    lines = [f"## cert-manager Certificates expiring within {horizon:.0f} days"]
    certificates = kube.list("cert-manager.io/v1", "Certificate")
    if not certificates:
        lines.append("unavailable - no Certificate objects readable.")
    else:
        rows = []
        for obj in certificates:
            metadata = obj.get("metadata") or {}
            status = obj.get("status") or {}
            not_after = status.get("notAfter")
            remaining = render.days_until(not_after)
            ready = _condition(status, "Ready")
            rows.append(
                [
                    f"{metadata.get('namespace')}/{metadata.get('name')}",
                    ready,
                    render.number(remaining, 1, "d") if remaining is not None else "unavailable",
                    "EXPIRING" if remaining is not None and remaining <= horizon else "ok",
                ]
            )
        rows.sort(key=lambda row: row[2])
        lines += render.table(["certificate", "ready", "expires in", "state"], rows)
        flagged = [row for row in rows if row[3] == "EXPIRING"]
        lines.append(
            f"{len(flagged)} certificate(s) inside the window."
            if flagged
            else "No certificate is inside the window."
        )

    lines.append("")
    lines.append("## ServiceAccount token Secrets")
    # Narrowed at the *request*, not after the fact: an unfiltered cluster-wide
    # Secret list pulls every value in every namespace over the wire.
    tokens = kube.list(
        "v1", "Secret", field_selector="type=kubernetes.io/service-account-token"
    )
    if not tokens:
        lines.append(
            
                "None. Every ServiceAccount token in use is a projected, auto-rotated "
                "volume rather than a stored Secret, which is the modern default and "
                "means there is nothing here to expire."
            
        )
    else:
        rows = [
            [
                (
                    f"{(obj.get('metadata') or {}).get('namespace')}/"
                    f"{(obj.get('metadata') or {}).get('name')}"
                ),
                render.age((obj.get("metadata") or {}).get("creationTimestamp")),
            ]
            for obj in tokens[:_RECENT]
        ]
        lines += render.table(["secret", "age"], rows)
    lines.append(
        "Only names, types and ages are read here. This server has no code path "
        "that can return a Secret's contents."
    )

    return truncate_lines(lines, BUDGET, unit="lines")


def _condition(status: dict, kind: str) -> str:
    for condition in status.get("conditions") or []:
        if condition.get("type") == kind:
            return str(condition.get("status", "-"))
    return "-"


def backup_freshness() -> str:
    limits = settings.thresholds("lifecycle")
    stale_hours = float(limits.get("backup_stale_warn_hours", 26))
    kube = settings.kube()

    lines: list[str] = []

    # -- Velero -------------------------------------------------------------
    lines.append("## Velero")
    schedules = kube.list("velero.io/v1", "Schedule")
    backups = kube.list("velero.io/v1", "Backup")
    if not schedules and not backups:
        lines.append("unavailable - no Velero objects readable.")
    else:
        lines += _schedule_rows(
            schedules,
            backups,
            group_label="velero.io/schedule-name",
            success_phases={"Completed"},
            completion=("status", "completionTimestamp"),
            paused=lambda obj: bool((obj.get("spec") or {}).get("paused")),
            stale_hours=stale_hours,
        )

    # -- CloudNativePG ------------------------------------------------------
    lines.append("")
    lines.append("## CloudNativePG")
    cnpg_scheduled = kube.list("postgresql.cnpg.io/v1", "ScheduledBackup")
    cnpg_backups = kube.list("postgresql.cnpg.io/v1", "Backup")
    if not cnpg_scheduled and not cnpg_backups:
        lines.append("unavailable - no CloudNativePG backup objects readable.")
    else:
        newest = _newest(cnpg_backups, {"completed"}, ("status", "stoppedAt"))
        failed = _count_failed(cnpg_backups, {"completed"}, ("status", "stoppedAt"))
        for obj in cnpg_scheduled:
            metadata = obj.get("metadata") or {}
            spec = obj.get("spec") or {}
            suspended = bool(spec.get("suspend"))
            lines.append(
                f"{metadata.get('namespace')}/{metadata.get('name')}: "
                f"schedule {spec.get('schedule', '-')}"
                + (" [SUSPENDED]" if suspended else "")
            )
        age_text, age_hours = newest
        lines.append(
            f"Newest completed backup: {age_text}"
            + (
                f"  STALE (threshold {stale_hours:.0f}h)"
                if age_hours is not None and age_hours > stale_hours
                else ""
            )
        )
        lines.append(f"Failed among the {_RECENT} most recent: {failed}.")
        lines.append(f"({len(cnpg_backups)} Backup objects exist; summarised, not listed.)")

    # -- Longhorn -----------------------------------------------------------
    lines.append("")
    lines.append("## Longhorn volume backups")
    lines += _longhorn(stale_hours)

    lines.append("")
    lines.append(
        "A backup system that stopped is worse than one that never existed, because "
        "everybody assumes it is working. An 'unavailable' above is an unknown "
        "backup state, not a healthy one."
    )
    return truncate_lines(lines, BUDGET, unit="lines")


def _schedule_rows(
    schedules: list[dict],
    backups: list[dict],
    *,
    group_label: str,
    success_phases: set[str],
    completion: tuple[str, ...],
    paused,
    stale_hours: float,
) -> list[str]:
    """One row per schedule: newest success, its age, recent failures."""
    grouped: dict[str, list[dict]] = {}
    for obj in backups:
        labels = (obj.get("metadata") or {}).get("labels") or {}
        grouped.setdefault(labels.get(group_label, "(ad-hoc)"), []).append(obj)

    rows = []
    for schedule in schedules:
        metadata = schedule.get("metadata") or {}
        name = metadata.get("name")
        members = grouped.pop(name, [])
        age_text, age_hours = _newest(members, success_phases, completion)
        state = "PAUSED" if paused(schedule) else (
            "STALE" if age_hours is None or age_hours > stale_hours else "ok"
        )
        rows.append(
            [
                f"{metadata.get('namespace')}/{name}",
                (schedule.get("spec") or {}).get("schedule", "-"),
                age_text,
                str(_count_failed(members, success_phases, completion)),
                state,
            ]
        )
    lines = render.table(["schedule", "cron", "newest success", "recent fail", "state"], rows)
    orphans = sum(len(items) for items in grouped.values())
    lines.append(
        f"({len(backups)} Backup objects exist across all schedules; summarised, not "
        f"listed. {orphans} not owned by a live schedule.)"
    )
    lines.append(f"STALE threshold is {stale_hours:.0f}h since the newest success.")
    return lines


def _newest(
    objects: list[dict], success_phases: set[str], completion: tuple[str, ...]
) -> tuple[str, float | None]:
    """``(rendered age, hours)`` of the newest successful object."""
    best: str | None = None
    for obj in objects:
        status = obj.get("status") or {}
        if str(status.get("phase", "")).lower() not in {phase.lower() for phase in success_phases}:
            continue
        cursor: object = obj
        for step in completion:
            cursor = (cursor or {}).get(step) if isinstance(cursor, dict) else None
        if isinstance(cursor, str) and (best is None or cursor > best):
            best = cursor
    if best is None:
        return ("none found", None)
    hours = render.days_until(best)
    return (render.age(best), abs(hours * 24) if hours is not None else None)


def _count_failed(
    objects: list[dict], success_phases: set[str], completion: tuple[str, ...]
) -> int:
    """Genuine failures among the most recent ``_RECENT``, newest first.

    "Not succeeded" is not "failed": an in-progress backup has not failed, and
    counting it as one inflates the number the report leads with. Anything
    unrecognised is left out rather than guessed at.
    """
    ordered = sorted(
        objects,
        key=lambda obj: (obj.get("metadata") or {}).get("creationTimestamp") or "",
        reverse=True,
    )[:_RECENT]
    return sum(
        1
        for obj in ordered
        if str((obj.get("status") or {}).get("phase", "")).lower() in _FAILED_PHASES
    )


def _longhorn(stale_hours: float) -> list[str]:
    """Newest backup age per Longhorn volume, from the operator's metric.

    Where the feature is not in use every volume reports 0, and calling that "N
    volumes never backed up" would be a finding that fires every run and can
    never be cleared. So nothing-backed-up reads as "not in use", and only a
    partial rollout is a finding.
    """
    reading = settings.prometheus().reading(
        "max by (volume) (longhorn_volume_last_backup_at)", "volume"
    )
    if not reading.ok:
        return [
            (
                "unavailable - the Longhorn backup query failed, so volume backup "
                "state is unknown."
            )
        ]
    values = reading.values
    if not values:
        return [
            (
                "Not reported - longhorn_volume_last_backup_at has no series, so "
                "either Longhorn is not installed or its metrics are not scraped."
            )
        ]

    backed_up = {volume: value for volume, value in values.items() if value}
    never = sorted(volume for volume, value in values.items() if not value)

    if not backed_up:
        return [
            (
                f"Not in use - none of the {len(values)} Longhorn volumes has a "
                "backup timestamp. Volume-level Longhorn backup appears not to be "
                "configured, which is a deliberate choice to make once and not a "
                "finding to repeat. Velero and CloudNativePG above are the backup "
                "path in use."
            )
        ]

    rows = []
    stale = []
    for volume, value in sorted(backed_up.items()):
        timestamp = _epoch_to_iso(value)
        age_days = render.days_until(timestamp)
        age_hours = -age_days * 24 if age_days is not None else None
        state = "STALE" if age_hours is not None and age_hours > stale_hours else "ok"
        if state == "STALE":
            stale.append(volume)
        rows.append([volume, render.age(timestamp), state])
    lines = render.table(["volume", "newest backup", "state"], rows)
    if never:
        lines.append(
            f"{len(never)} of {len(values)} volume(s) have no backup while others do, "
            "which is a partial rollout rather than a setting: "
            + ", ".join(never[:6])
            + (", ..." if len(never) > 6 else "")
        )
    if stale:
        lines.append(
            f"{len(stale)} volume(s) have a backup older than {stale_hours:.0f}h: "
            + ", ".join(stale[:6])
            + (", ..." if len(stale) > 6 else "")
        )
    lines.append(f"STALE threshold is {stale_hours:.0f}h.")
    return lines


def _epoch_to_iso(value: float) -> str:
    import datetime

    # The metric is a unix timestamp in seconds; some builds publish milliseconds.
    seconds = value / 1000 if value > 1e11 else value
    return datetime.datetime.fromtimestamp(seconds, datetime.UTC).isoformat()


CERT_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
BACKUP_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

CERT_DESCRIPTION = """
Certificates and token Secrets approaching expiry, with the number of days
remaining already computed.

Takes no arguments. You do not need to know today's date - the days-remaining
figure is a tool result. A negative value means it has already expired.
"""

BACKUP_DESCRIPTION = """
Whether each backup system last succeeded recently enough: Velero, CloudNativePG
and Longhorn, one row per schedule with the age of the newest success.

Takes no arguments. Individual backup objects are summarised rather than listed -
there are hundreds, and the question is when each schedule last succeeded.
"""
