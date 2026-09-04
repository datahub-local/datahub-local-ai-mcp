"""`why_failed()`, `logs()` and `endpoints()` - an investigation per call.

`find_object` fixed the first half of the grafana incident: nobody types an exact
name, so the name is now returned rather than guessed. The second half was still
the model's to run - object, then pods, then containers, then events, then logs -
and it is four calls of exact arguments in a fixed order, which is the shape this
fleet has failed at repeatedly. `sre-sentinel` spent five lookups on one alert and
landed none; `db-steward` spent seven on one Cluster; the oracle spent eight and
concluded that a running grafana did not exist.

So the chain runs here. A pod is reached from its owner's own `ownerReferences`
or its controller's own `spec.selector`, never from a label key a model chose,
and every step that could not run says so in those words instead of collapsing
into an absence. The verdict is assembled from what was read, and "nothing here
is failing" is one of the answers - a report format that demands a fault is how
invented ones get written.
"""

from __future__ import annotations

from mcp_runner import kube, render
from mcp_runner import loki as loki_module
from mcp_runner.budget import truncate_lines

from .. import settings
from . import lookup

BUDGET = 4096
LOGS_BUDGET = 4096
ENDPOINTS_BUDGET = 3072

# What can fail, in the sense a person means by "why did X fail". A Namespace, a
# Node or a PVC is not investigated this way, so they are not searched: a term
# that only matches one of those is better answered by `find_object`.
_FAILABLE: tuple[tuple[str, str], ...] = (
    ("v1", "Service"),
    ("apps/v1", "Deployment"),
    ("apps/v1", "StatefulSet"),
    ("apps/v1", "DaemonSet"),
    ("batch/v1", "CronJob"),
    ("argoproj.io/v1alpha1", "Application"),
    ("batch/v1", "Job"),
    ("v1", "Pod"),
)

# What has logs. A Service does, transitively, through the pods it selects -
# which is how "what is grafana logging" is answered without anyone knowing a
# pod name.
_LOGGABLE: tuple[tuple[str, str], ...] = (
    ("v1", "Service"),
    ("apps/v1", "Deployment"),
    ("apps/v1", "StatefulSet"),
    ("apps/v1", "DaemonSet"),
    ("batch/v1", "CronJob"),
    ("batch/v1", "Job"),
    ("v1", "Pod"),
)

# What answers "can it be reached".
_REACHABLE: tuple[tuple[str, str], ...] = (
    ("v1", "Service"),
    ("traefik.io/v1alpha1", "IngressRoute"),
    ("networking.k8s.io/v1", "Ingress"),
)

# Bounds. Few calls, small answers: one oversized result reproducibly ends a run
# with no report at all.
_MAX_PODS = 3
_MAX_EVENTS = 6
_MAX_LOG_LINES = 12
_MAX_LINE_CHARS = 200
_LOGS_TAIL = 30
_LOKI_WINDOW_SECONDS = 21600  # 6h, stated in the output rather than assumed

# Container states that are a cause on their own. The message beside each is what
# the API server already knows, so nothing here needs to be inferred.
_FATAL_WAITING = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "CreateContainerError",
    "InvalidImageName",
}


def _clip(line: str) -> str:
    line = line.rstrip()
    return line if len(line) <= _MAX_LINE_CHARS else line[:_MAX_LINE_CHARS] + " ...[line cut]"


def _labels(obj: dict) -> dict:
    return ((obj.get("metadata") or {}).get("labels")) or {}


def _uid(obj: dict) -> str:
    return ((obj.get("metadata") or {}).get("uid")) or ""


def _owned_by(obj: dict, uid: str) -> bool:
    return any(
        (owner or {}).get("uid") == uid
        for owner in ((obj.get("metadata") or {}).get("ownerReferences") or [])
    )


def _selects(pod: dict, selector: dict) -> bool:
    """Does ``pod`` carry every label in ``selector``?

    The selector is read off the controller that owns the pods, so this is the
    cluster's own answer to which pods belong to it. No label key originates
    here, which is the whole point.
    """
    if not selector:
        return False
    labels = _labels(pod)
    return all(labels.get(key) == value for key, value in selector.items())


def pods_for(client, match: lookup.Match) -> tuple[list[dict], str]:
    """The pods behind a matched object, and how they were found.

    Returns ``([], reason)`` when there are none - which is a finding in its own
    right for a Deployment that scaled to zero or a Service selecting nothing,
    and is never reported as the object not existing.
    """
    if match.kind == "Pod":
        return [match.obj], "the pod itself"

    namespace = match.namespace
    pods = client.list("v1", "Pod", namespace=namespace)

    if match.kind == "Job":
        uid = _uid(match.obj)
        return [pod for pod in pods if _owned_by(pod, uid)], f"owned by Job {match.name}"

    if match.kind == "CronJob":
        uid = _uid(match.obj)
        jobs = [job for job in client.list("batch/v1", "Job", namespace=namespace)
                if _owned_by(job, uid)]
        jobs.sort(key=lambda job: (job.get("metadata") or {}).get("creationTimestamp") or "",
                  reverse=True)
        job_uids = {_uid(job) for job in jobs[:2]}
        owned = [pod for pod in pods if any(_owned_by(pod, uid) for uid in job_uids)]
        return owned, f"owned by the 2 newest Jobs of CronJob {match.name}"

    if match.kind in {"Deployment", "StatefulSet", "DaemonSet"}:
        selector = ((match.obj.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        return (
            [pod for pod in pods if _selects(pod, selector)],
            f"selected by the {match.kind}'s own spec.selector.matchLabels",
        )

    if match.kind == "Service":
        selector = (match.obj.get("spec") or {}).get("selector") or {}
        return (
            [pod for pod in pods if _selects(pod, selector)],
            "selected by the Service's own spec.selector",
        )

    return [], f"a {match.kind} has no pods of its own"


def _newest_first(objects: list[dict]) -> list[dict]:
    return sorted(
        objects,
        key=lambda obj: (obj.get("metadata") or {}).get("creationTimestamp") or "",
        reverse=True,
    )


def _container_rows(pod: dict) -> list[list[str]]:
    """One row per container: what it is doing and why, from the API server."""
    rows = []
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    for status in statuses:
        state = status.get("state") or {}
        if "running" in (state or {}):
            what = "running"
            detail = f"started {render.age((state.get('running') or {}).get('startedAt'))}"
        elif "waiting" in state:
            waiting = state.get("waiting") or {}
            what = f"waiting {waiting.get('reason') or 'unknown'}"
            detail = waiting.get("message") or "-"
        elif "terminated" in state:
            terminated = state.get("terminated") or {}
            what = f"terminated {terminated.get('reason') or 'unknown'}"
            detail = f"exit {terminated.get('exitCode')}"
        else:
            what = "unknown"
            detail = "-"
        rows.append(
            [
                status.get("name") or "-",
                what,
                "yes" if status.get("ready") else "no",
                str(status.get("restartCount") or 0),
                _clip(detail)[:120],
            ]
        )
    if not statuses:
        rows.append(["-", "no container status yet", "no", "0", "the pod has not started"])
    return rows


def _events(client, namespace: str, names: list[str]) -> list[str]:
    """Warning events for the named objects, newest first.

    `involvedObject.name` is a real field selector on this API, so the filter is
    the server's. Warnings first because a Normal event is the scheduler saying
    it did its job, which is never the answer to why something failed.
    """
    collected = []
    for name in names[: _MAX_PODS + 1]:
        try:
            found = client.list(
                "v1", "Event", namespace=namespace, field_selector=f"involvedObject.name={name}"
            )
        except kube.KubeForbidden:
            return ["events: NOT READ - this server is not permitted to list Events here."]
        except kube.KubeError as exc:
            return [f"events: ERROR - {exc}"]
        collected += found
    if not collected:
        return ["events: none recorded for these objects (Kubernetes keeps them ~1h)."]

    def stamp(event):
        return event.get("lastTimestamp") or event.get("eventTime") or ""

    warnings = [event for event in collected if event.get("type") == "Warning"]
    ordered = sorted(warnings or collected, key=stamp, reverse=True)[:_MAX_EVENTS]
    rows = [
        [
            (event.get("involvedObject") or {}).get("name") or "-",
            event.get("type") or "-",
            event.get("reason") or "-",
            f"x{event.get('count') or 1}",
            render.age(stamp(event)),
            _clip(event.get("message") or "-")[:110],
        ]
        for event in ordered
    ]
    return render.table(["object", "type", "reason", "count", "when", "message"], rows)


def _pick_container(pod: dict) -> tuple[str | None, bool]:
    """Which container to read, and whether to read its *previous* instance.

    A CrashLoopBackOff container is not running, so its current log is empty or
    absent - the log that explains it belongs to the instance that already died.
    Reading the wrong one returns nothing and reads as "it logged nothing".
    """
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    for status in statuses:
        state = status.get("state") or {}
        waiting = (state.get("waiting") or {}).get("reason")
        terminated = state.get("terminated") or {}
        restarts = int(status.get("restartCount") or 0)
        if waiting in _FATAL_WAITING or (terminated and terminated.get("exitCode")):
            return status.get("name"), bool(restarts) or bool(waiting)
        if not status.get("ready"):
            return status.get("name"), restarts > 0
    if statuses:
        return statuses[0].get("name"), False
    return None, False


def _log_tail(client, namespace: str, pod: str, container: str | None, previous: bool,
              lines: int, contains: str | None = None) -> tuple[list[str], str]:
    """Log lines for one container, live if possible and from Loki if not.

    Returns ``(lines, source)``. Every failure to read is carried in ``source``
    as its own sentence: an unreadable log is not an empty one.
    """
    attempts = [(previous, "the previous (crashed) instance"), (False, "the current instance")]
    if not previous:
        attempts = [(False, "the current instance")]
    notes = []
    for use_previous, what in attempts:
        try:
            text = client.pod_log(
                namespace, pod, container, tail_lines=lines, previous=use_previous
            )
        except kube.KubeForbidden as exc:
            return [], f"NOT READ: {exc}."
        except kube.KubeError as exc:
            notes.append(f"kubelet log of {what}: {exc}")
            continue
        kept = [line for line in text.splitlines() if line.strip()]
        if contains:
            kept = [line for line in kept if contains.lower() in line.lower()]
        if kept:
            return kept[-lines:], f"the kubelet, {what}"
        notes.append(f"kubelet log of {what}: no matching lines")

    entries, source = _loki_tail(namespace, pod, container, lines, contains)
    if entries:
        return entries, source
    return [], "; ".join(notes + [source]) if notes else source


def _loki_tail(namespace: str, pod: str | None, container: str | None, lines: int,
               contains: str | None) -> tuple[list[str], str]:
    """The same tail from Loki, which keeps containers the kubelet has dropped."""
    query = loki_module.stream_selector(namespace, pod, container)
    if contains:
        query = f"{query} {loki_module.contains_filter(contains)}"
    hours = _LOKI_WINDOW_SECONDS // 3600
    try:
        entries = settings.loki().query_range(
            query, since_seconds=_LOKI_WINDOW_SECONDS, limit=lines
        )
    except loki_module.LokiError as exc:
        return [], f"Loki could not be queried ({exc}), so retained logs are unknown."
    if not entries:
        return [], (
            f"Loki has no line matching {query} in the last {hours}h, which means "
            f"none was retained, not that the container was quiet."
        )
    return [entry.line for entry in reversed(entries)], f"Loki, last {hours}h"


def _verdict(match: lookup.Match, pods: list[dict], how: str) -> list[str]:
    """The cause, assembled from what was read - or the absence of one, said so.

    Both halves matter. A stated cause has to come from a field; "nothing here is
    failing right now" has to be available, or a mandatory finding gets invented
    to fill the space.
    """
    if match.kind == "Application":
        status = match.obj.get("status") or {}
        sync = (status.get("sync") or {}).get("status") or "unknown"
        health = (status.get("health") or {}).get("status") or "unknown"
        message = ((status.get("operationState") or {}).get("message")
                   or (status.get("health") or {}).get("message") or "")
        line = f"VERDICT: ArgoCD reports sync={sync}, health={health}."
        return [line + (f" ArgoCD's own message: {_clip(message)}" if message else "")]

    if match.kind == "Service" and not pods:
        return [
            (
                "VERDICT: this Service selects no pod at all, so nothing is behind it "
            "and a connection to it is refused. That is a wiring fault in the "
                "Service or a workload that is not running, not a missing Service."
            )
        ]

    if not pods:
        return [
            (
                f"VERDICT: cause not determined. No pod is {how}, so there is nothing "
            f"running to inspect. That is a real finding - a scaled-to-zero "
            f"workload or a Job with no surviving pod - and not evidence that "
                f"{match.name} does not exist."
            )
        ]

    problems: list[str] = []
    for pod in pods:
        name = (pod.get("metadata") or {}).get("name") or "-"
        phase = (pod.get("status") or {}).get("phase") or "unknown"
        for status in (pod.get("status") or {}).get("containerStatuses") or []:
            container = status.get("name") or "-"
            state = status.get("state") or {}
            waiting = (state.get("waiting") or {}).get("reason")
            terminated = state.get("terminated") or {}
            last = (status.get("lastState") or {}).get("terminated") or {}
            if waiting in _FATAL_WAITING:
                extra = ""
                if last.get("reason"):
                    extra = f" The previous instance ended {last.get('reason')}"
                    if last.get("exitCode") is not None:
                        extra += f" with exit code {last.get('exitCode')}"
                    extra += "."
                problems.append(f"{name}/{container} is {waiting}.{extra}")
            elif terminated.get("reason") == "OOMKilled" or last.get("reason") == "OOMKilled":
                problems.append(
                    f"{name}/{container} was OOMKilled - the kernel stopped it for "
                    f"exceeding its memory limit."
                )
            elif terminated and terminated.get("exitCode"):
                problems.append(
                    f"{name}/{container} exited with code {terminated.get('exitCode')} "
                    f"({terminated.get('reason') or 'no reason given'})."
                )
        if phase == "Pending":
            problems.append(f"{name} is Pending - it has not been scheduled onto a node.")

    if problems:
        return ["VERDICT: " + " ".join(problems[:4])]

    running = sum(1 for pod in pods if (pod.get("status") or {}).get("phase") == "Running")
    succeeded = sum(1 for pod in pods if (pod.get("status") or {}).get("phase") == "Succeeded")
    return [
        (
            f"VERDICT: nothing here is failing right now - of {len(pods)} pod(s), "
        f"{running} are Running and {succeeded} Succeeded, with no container in a "
        f"failure state. If a failure is being asked about, it is over: look at "
        f"the events and log lines above for what happened, and report that the "
            f"current state is healthy."
        )
    ]


def why_failed(term: str) -> str:
    """Object, pods, containers, events and the relevant log tail, in one call."""
    term = (term or "").strip()
    if not term:
        return "ERROR: term is required. Example: term=grafana setup job"

    client = settings.kube()
    matches, unreadable, errors = lookup.resolve(client, term, _FAILABLE)
    if not matches:
        return lookup.not_matched(term, _FAILABLE, unreadable)

    primary = matches[0]
    lines: list[str] = [
        (
            f"Investigation of '{term}' -> {primary.kind} "
            f"{primary.namespace}/{primary.name} (matched {primary.how})."
        ),
    ]
    others = [m for m in matches[1:] if (m.kind, m.name) != (primary.kind, primary.name)]
    if others:
        lines.append(
            "Also matched, not investigated: "
            + ", ".join(f"{m.kind} {m.namespace}/{m.name}" for m in others[:5])
            + (f" and {len(others) - 5} more" if len(others) > 5 else "")
            + ". Ask again with a more specific term to investigate one of these."
        )
    lines += errors
    if unreadable:
        lines.append(
            "NOT SEARCHED, because this server may not list them: "
            + ", ".join(unreadable)
            + ". A match there would not have been seen."
        )
    lines.append("")

    pods, how = pods_for(client, primary)
    pods = _newest_first(pods)
    shown = pods[:_MAX_PODS]

    lines.append(f"## Pods ({len(pods)} found, {how})")
    if shown:
        rows = []
        for pod in shown:
            metadata = pod.get("metadata") or {}
            phase = (pod.get("status") or {}).get("phase") or "unknown"
            for row in _container_rows(pod):
                rows.append([metadata.get("name") or "-", phase] + row)
        lines += render.table(
            ["pod", "phase", "container", "state", "ready", "restarts", "detail"], rows
        )
        if len(pods) > _MAX_PODS:
            lines.append(f"({len(pods) - _MAX_PODS} more pods not shown; the newest are here.)")
    else:
        lines.append("(none)")
    lines.append("")

    # Ordered and deduplicated rather than a set: the pods come first because
    # they carry the failure, and a set would drop a different name each run.
    names: list[str] = []
    for name in [(pod.get("metadata") or {}).get("name") for pod in shown] + [primary.name]:
        if name and name not in names:
            names.append(name)
    lines.append("## Recent events")
    lines += _events(client, primary.namespace, names)
    lines.append("")

    if shown:
        target = shown[0]
        pod_name = (target.get("metadata") or {}).get("name") or ""
        container, previous = _pick_container(target)
        tail, source = _log_tail(
            client, primary.namespace, pod_name, container, previous, _MAX_LOG_LINES
        )
        label = f"{pod_name}/{container or 'default container'}"
        lines.append(f"## Log tail of {label}")
        lines.append(f"(source: {source})")
        lines += [_clip(line) for line in tail]
        lines.append("")

    lines += _verdict(primary, pods, how)
    return truncate_lines(lines, BUDGET, unit="lines")


def logs(term: str, contains: str = "") -> str:
    """The recent log of whatever ``term`` names, live or from Loki."""
    term = (term or "").strip()
    contains = (contains or "").strip()
    if not term:
        return "ERROR: term is required. Example: term=grafana"

    client = settings.kube()
    matches, unreadable, errors = lookup.resolve(client, term, _LOGGABLE)
    if not matches:
        return lookup.not_matched(term, _LOGGABLE, unreadable)

    primary = matches[0]
    pods, how = pods_for(client, primary)
    pods = _newest_first(pods)

    filtered = f" containing '{contains}'" if contains else ""
    lines: list[str] = [
        (
            f"Logs for '{term}'{filtered} -> {primary.kind} "
            f"{primary.namespace}/{primary.name} (matched {primary.how})."
        ),
    ]
    lines += errors
    if unreadable:
        lines.append(
            "NOT SEARCHED: " + ", ".join(unreadable) + " (this server may not list them)."
        )

    if not pods:
        # Still a real answer: the namespace stream is what a deleted pod leaves
        # behind, and saying "no logs" here would be the absence bug again.
        tail, source = _loki_tail(primary.namespace, None, None, _MAX_LOG_LINES, contains or None)
        lines.append(
            f"No pod is {how}, so there is no live container to read. "
            f"Falling back to everything {primary.namespace} logged."
        )
        lines.append(f"(source: {source})")
        lines += [_clip(line) for line in tail]
        return truncate_lines(lines, LOGS_BUDGET, unit="lines")

    target = pods[0]
    pod_name = (target.get("metadata") or {}).get("name") or ""
    if len(pods) > 1:
        others = [(pod.get("metadata") or {}).get("name") or "-" for pod in pods[1:]]
        listed = ", ".join(others[:_MAX_PODS])
        rest = f", and {len(others) - _MAX_PODS} more" if len(others) > _MAX_PODS else ""
        lines.append(
            f"{len(pods)} pods are {how}. Reading the newest, {pod_name}. "
            f"The others: {listed}{rest}. Name one of them to read it instead."
        )
    container, previous = _pick_container(target)
    tail, source = _log_tail(
        client, primary.namespace, pod_name, container, previous, _LOGS_TAIL, contains or None
    )
    lines.append(f"## {primary.namespace}/{pod_name}/{container or 'default container'}")
    lines.append(f"(source: {source})")
    lines += [_clip(line) for line in tail]
    return truncate_lines(lines, LOGS_BUDGET, unit="lines")


def endpoints(term: str) -> str:
    """Whether what ``term`` names can actually be connected to."""
    term = (term or "").strip()
    if not term:
        return "ERROR: term is required. Example: term=grafana"

    client = settings.kube()
    matches, unreadable, errors = lookup.resolve(client, term, _REACHABLE)
    if not matches:
        return lookup.not_matched(term, _REACHABLE, unreadable)

    lines: list[str] = [
        (
            f"Reachability of '{term}'. `curl: (7) could not connect` on a name "
            f"that resolves is a Service with no ready endpoint, never a missing "
            f"Service; exit 6 is the DNS failure."
        ),
        "",
    ]
    lines += errors
    if unreadable:
        lines.append("NOT SEARCHED: " + ", ".join(unreadable) + ".")

    services = [match for match in matches if match.kind == "Service"][:4]
    routes = [match for match in matches if match.kind in {"IngressRoute", "Ingress"}][:4]

    for match in services:
        spec = match.obj.get("spec") or {}
        ports = ",".join(str(port.get("port")) for port in (spec.get("ports") or [])) or "-"
        lines.append(f"## Service {match.namespace}/{match.name} (ports {ports})")
        lines += lookup.endpoint_lines(client, [(match.namespace, match.name)])
        pods, how = pods_for(client, match)
        if not pods:
            lines.append(
                f"No pod carries the labels this Service selects ({spec.get('selector') or '{}'}), "
                f"which is why there is nothing behind it."
            )
        else:
            rows = [
                [
                    (pod.get("metadata") or {}).get("name") or "-",
                    (pod.get("status") or {}).get("phase") or "unknown",
                    "yes" if _pod_ready(pod) else "NO",
                ]
                for pod in _newest_first(pods)[:_MAX_PODS]
            ]
            lines.append(f"Pods {how}:")
            lines += render.table(["pod", "phase", "ready"], rows)
        lines.append("")

    for match in routes:
        lines.append(f"## {match.kind} {match.namespace}/{match.name}")
        lines.append(lookup.state_of(match.kind, match.obj))
        lines.append("")

    return truncate_lines(lines, ENDPOINTS_BUDGET, unit="lines")


def _pod_ready(pod: dict) -> bool:
    conditions = (pod.get("status") or {}).get("conditions") or []
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in conditions
    )


_TERM_PROPERTY = {
    "type": "string",
    "description": "The word a person used, e.g. grafana, or several words such "
    "as grafana setup job. Case and punctuation are ignored and no part of the "
    "name has to be exact.",
}

WHY_FAILED_SCHEMA = {
    "type": "object",
    "properties": {"term": _TERM_PROPERTY},
    "required": ["term"],
    "additionalProperties": False,
}

LOGS_SCHEMA = {
    "type": "object",
    "properties": {
        "term": _TERM_PROPERTY,
        "contains": {
            "type": "string",
            "description": "Optional. Keep only lines containing this text, "
            "matched literally and case-insensitively. Any text is valid.",
        },
    },
    "required": ["term"],
    "additionalProperties": False,
}

ENDPOINTS_SCHEMA = {
    "type": "object",
    "properties": {"term": _TERM_PROPERTY},
    "required": ["term"],
    "additionalProperties": False,
}

WHY_FAILED_DESCRIPTION = """
Why something failed: its pods, their containers, the events and the log tail.

Call this for any question about a failure, a crash, a restart, an error or
something not working, naming it however the person did. It resolves the name
itself, follows the object to its pods through their own owner references or
selector, reads each container's state, the recent Warning events and the log of
the container that actually broke - including the previous instance when it is
crash-looping, which is where the reason is.

Ends with a VERDICT line assembled from what was read. "nothing here is failing
right now" is one of its answers and is a complete one; so is "cause not
determined", which is a real finding and not a reason to keep looking.
"""

LOGS_DESCRIPTION = """
The recent log of whatever you name, live from the container or from Loki.

Resolves the name to a pod itself and picks the container, so no pod name,
namespace, container, LogQL selector or datasource is ever supplied by you. Reads
the running container first and falls back to Loki, which keeps containers that
no longer exist. `contains` optionally keeps only matching lines.

It always says which source answered and over what window. "no line was
retained" is a stated answer and never means the container was quiet.
"""

ENDPOINTS_DESCRIPTION = """
Whether a service can be connected to, and what is behind it.

Call this for `could not connect`, `connection refused`, `502`, `503` or "is X
up". Reports each matching Service's ready and not-ready endpoint counts, the
pods its own selector picks and whether they are ready, and any Traefik
IngressRoute's hostnames.

A Service with zero ready endpoints is refusing connections and is not a missing
Service - which is what a `curl: (7)` on a name that resolved actually means.
"""
