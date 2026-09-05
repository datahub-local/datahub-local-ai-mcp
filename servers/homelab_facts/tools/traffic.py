"""`top_services()` and `workload_readiness()` - who is being used, and what is short of pods.

Nothing here names an ingress controller. The metric, the label carrying the
service name and the cosmetic trimming of that name are all configured, because
each differs per controller: Traefik counts into `traefik_service_requests_total`
and nginx into `nginx_ingress_controller_requests`, with different label names
again. A default would be a claim about which controller a cluster runs.

Two settings are required for the same reason `PROMETHEUS_URL` is: a guessed
metric name matches no series, and "no series" renders as "nobody uses anything"
- a wrong reading in the shape of an empty one. The trimming regex is optional
because a name that is merely ugly still reports correctly.

The rate window is applied in code because a small model handed `rate(m[24h])`
sends `m[24h]` and labels the answer a rate.
"""

from __future__ import annotations

import logging
import re

from mcp_runner import config as runner_config
from mcp_runner import render
from mcp_runner.budget import truncate_lines
from mcp_runner.prometheus import PrometheusError

from .. import settings

logger = logging.getLogger(__name__)

BUDGET = 2560
READINESS_BUDGET = 2048

_TOP_N = 12

# Fixed, not an argument. A window has exactly one correct shape ("24h", never
# "24 hours"), which is the class of argument this server does not take.
_WINDOW = "24h"

_METRIC_VAR = "INGRESS_REQUESTS_METRIC"
_LABEL_VAR = "INGRESS_SERVICE_LABEL"
_STRIP_VAR = "INGRESS_SERVICE_STRIP_PATTERN"
_ERROR_LABEL_VAR = "INGRESS_STATUS_LABEL"


def requests_metric() -> str:
    """The counter the ingress controller increments per request."""
    value = runner_config.env(_METRIC_VAR)
    if not value:
        raise ValueError(
            f"{_METRIC_VAR} is unset, so there is no request counter to read. Set it to "
            f"the per-request counter your ingress controller exports, e.g. "
            f"'traefik_service_requests_total' or 'nginx_ingress_controller_requests'. "
            f"There is deliberately no default: a guessed metric matches no series and "
            f"reports as a fleet nobody uses."
        )
    return value


def service_label() -> str:
    """The label on that counter carrying the backend service name.

    Required because it is not guessable even knowing the controller: a
    ServiceMonitor that already owns `service` makes Prometheus relabel the
    exporter's own to `exported_service`, and grouping on the wrong one returns
    a single plausible row covering the whole fleet.
    """
    value = runner_config.env(_LABEL_VAR)
    if not value:
        raise ValueError(
            f"{_LABEL_VAR} is unset, so there is no label to group requests by. Set it to "
            f"the label carrying the backend name, e.g. 'exported_service' or 'service'. "
            f"There is deliberately no default: grouping on a label the metric does not "
            f"carry collapses every service into one row rather than failing."
        )
    return value


def status_label() -> str:
    """The label carrying the HTTP status. Optional; without it the error
    column is omitted rather than guessed."""
    return runner_config.env(_ERROR_LABEL_VAR, "code")


def _strip_pattern() -> re.Pattern[str] | None:
    """Cosmetic trimming of the service name, e.g. a provider suffix and config
    hash. Optional - an untrimmed name is ugly, not wrong - but a name that
    changes between runs breaks every comparison a caller makes against it."""
    raw = runner_config.env(_STRIP_VAR)
    if not raw:
        return None
    try:
        return re.compile(raw)
    except re.error as exc:
        logger.warning(
            "%s is not a valid regular expression (%s): reporting service names "
            "untrimmed. Fix the pattern or unset it.",
            _STRIP_VAR,
            exc,
        )
        return None


def clean_service(raw: str, pattern: re.Pattern[str] | None) -> str:
    """Apply the configured trimming, never returning an empty name."""
    if pattern is None:
        return raw
    return pattern.sub("", raw) or raw


def build_rate_expression(metric: str, label: str, window: str) -> str:
    """The request-rate expression. Separate so a test can assert the rate
    wrapper and the grouping label without a Prometheus."""
    return f"sum by ({label}) (rate({metric}[{window}]))"


def build_error_expression(metric: str, label: str, status: str, window: str) -> str:
    """Share of 5xx per service, as a percentage of that service's own requests."""
    return (
        "100 * "
        f'sum by ({label}) (rate({metric}{{{status}=~"5.."}}[{window}]))'
        " / "
        f"sum by ({label}) (rate({metric}[{window}]))"
    )


def _by_service(series: list[dict], label: str, pattern: re.Pattern[str] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in series:
        raw = (item.get("metric") or {}).get(label)
        if not raw:
            continue
        try:
            out[clean_service(raw, pattern)] = float(item["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


def top_services() -> str:
    """Rank HTTP services by request rate over the fixed window."""
    try:
        metric = requests_metric()
        label = service_label()
    except ValueError as exc:
        return f"ERROR: {exc}"

    pattern = _strip_pattern()
    prometheus = settings.prometheus()

    try:
        rates = _by_service(
            prometheus.instant(build_rate_expression(metric, label, _WINDOW)), label, pattern
        )
    except PrometheusError as exc:
        return f"ERROR: request-rate query failed: {exc}"

    if not rates:
        return (
            f"No HTTP traffic series matched over {_WINDOW}. This is an empty result, "
            "not a fleet nobody is using - treat it as unavailable.\n"
            f"metric: {metric}, grouped by: {label}\n"
            "Either the ingress controller exports no Prometheus metrics, or "
            f"{_METRIC_VAR}/{_LABEL_VAR} name something this Prometheus does not carry."
        )

    status = status_label()
    try:
        errors = _by_service(
            prometheus.instant(build_error_expression(metric, label, status, _WINDOW)),
            label,
            pattern,
        )
        errors_read = True
    except PrometheusError:
        errors, errors_read = {}, False

    ranked = sorted(rates, key=lambda key: -rates[key])
    shown = ranked[:_TOP_N]

    rows = []
    for name in shown:
        per_second = rates[name]
        if errors_read:
            share = errors.get(name)
            error_column = render.number(share, 1, "%") if share is not None else "0.0%"
        else:
            error_column = render.number(None)
        rows.append(
            [
                name,
                render.number(per_second, 3, "/s"),
                render.number(per_second * 3600, 0, "/h"),
                error_column,
            ]
        )

    total = sum(rates.values())
    lines = [
        (
            f"Top HTTP services by request rate over {_WINDOW}, {len(rates)} routed "
            f"service(s) answering, {render.number(total, 3, '/s')} total."
        ),
        (
            "Rates are already averaged over the window. The error column is the "
            "share of that service's own requests answered 5xx, not a share of the "
            "fleet - a low-traffic service at 100% is a handful of requests."
        ),
    ]
    lines += render.table(["service", "rate", "per hour", "5xx"], rows)
    if len(ranked) > _TOP_N:
        lines.append(f"{len(ranked) - _TOP_N} quieter service(s) not shown.")
    lines.append(
        "A service missing from this table is not reached through the ingress "
        "controller - internal-only traffic is not counted anywhere here."
    )
    return truncate_lines(lines, BUDGET, unit="services")


_KINDS = (
    (
        "Deployment",
        "kube_deployment_status_replicas_available",
        "kube_deployment_spec_replicas",
        "deployment",
        "on(namespace, deployment)",
    ),
    (
        "StatefulSet",
        "kube_statefulset_status_replicas_ready",
        "kube_statefulset_replicas",
        "statefulset",
        "on(namespace, statefulset)",
    ),
    (
        "DaemonSet",
        "kube_daemonset_status_number_ready",
        "kube_daemonset_status_desired_number_scheduled",
        "daemonset",
        "on(namespace, daemonset)",
    ),
)


def build_shortfall_expression(ready: str, desired: str, join: str) -> str:
    """Workloads with fewer ready pods than they want. The `on()` join is what
    keeps kube-state-metrics' differing label sets from dropping every row."""
    return f"{ready} < {join} {desired}"


def workload_readiness() -> str:
    """Every Deployment, StatefulSet and DaemonSet short of its wanted pods."""
    prometheus = settings.prometheus()
    rows = []
    checked = 0
    failed = []

    for kind, ready_metric, desired_metric, label, join in _KINDS:
        try:
            total = prometheus.instant(f"count({desired_metric})")
            checked += int(float(total[0]["value"][1])) if total else 0
        except (PrometheusError, KeyError, IndexError, TypeError, ValueError):
            failed.append(kind)
            continue

        try:
            short = prometheus.instant(
                build_shortfall_expression(ready_metric, desired_metric, join)
            )
        except PrometheusError:
            failed.append(kind)
            continue

        for item in short:
            metric = item.get("metric") or {}
            namespace = metric.get("namespace")
            name = metric.get(label)
            if not namespace or not name:
                continue
            try:
                have = float(item["value"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            want = _lookup(prometheus, desired_metric, label, namespace, name)
            rows.append(
                [
                    kind,
                    f"{namespace}/{name}",
                    f"{render.number(have, 0)}/{render.number(want, 0)}",
                    "none ready" if have == 0 else "degraded",
                ]
            )

    if failed and not checked:
        return (
            "ERROR: no workload series could be read ("
            + ", ".join(failed)
            + "). kube-state-metrics is the source; treat this as unavailable, "
            "not as a healthy cluster."
        )

    lines = [f"Workload readiness across {checked} workload(s)."]
    if failed:
        lines.append(f"Not checked, query failed: {', '.join(failed)}.")
    if rows:
        lines.append(
            f"{len(rows)} workload(s) with fewer ready pods than wanted. "
            "'none ready' means the workload is serving nothing."
        )
        lines += render.table(["kind", "workload", "ready/wanted", "state"], rows)
    else:
        lines.append("Every workload has all of its wanted pods ready.")
    return truncate_lines(lines, READINESS_BUDGET, unit="workloads")


def _lookup(prometheus, metric: str, label: str, namespace: str, name: str) -> float | None:
    try:
        series = prometheus.instant(f'{metric}{{namespace="{namespace}",{label}="{name}"}}')
        return float(series[0]["value"][1]) if series else None
    except (PrometheusError, KeyError, IndexError, TypeError, ValueError):
        return None


SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

READINESS_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

DESCRIPTION = """
Top HTTP services by request rate, busiest first, with each one's 5xx share.

Takes no arguments; the rate is averaged over the last 24h and the service names
are already cleaned - report the rows as given. Only traffic through the ingress
controller is counted; internal service-to-service calls are invisible here and
their absence is not an idle service.
"""

READINESS_DESCRIPTION = """
Every Deployment, StatefulSet and DaemonSet with fewer ready pods than it wants.

Takes no arguments. An empty table means every workload is fully ready, which is
a real finding. 'none ready' means the workload is serving nothing at all and is
the more urgent of the two states.
"""
