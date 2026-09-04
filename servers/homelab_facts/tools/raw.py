"""`promql()` - arbitrary Prometheus, with everything unguessable supplied here.

One argument. There is no datasource, no time and no query-type to get wrong,
which is what lets the shared PromQL prompt text be deleted outright.
"""

from __future__ import annotations

from mcp_runner import render
from mcp_runner.budget import truncate_lines
from mcp_runner.prometheus import PrometheusError

from .. import settings

# Smaller than the default: a hand-written query can match thousands of series,
# and this is the one tool whose result size the caller chooses.
BUDGET = 3072

_MAX_SERIES = 40


def promql(expr: str, window: str = "") -> str:
    """Run one PromQL query and return the samples as a table."""
    expr = (expr or "").strip()
    if not expr:
        return "ERROR: expr is required. Example: expr=up"

    prometheus = settings.prometheus()
    try:
        if window:
            seconds = _parse_window(window)
            series = prometheus.range_(expr, since_seconds=seconds, step_seconds=max(60, seconds // 60))
            return _render_range(expr, window, series)
        series = prometheus.instant(expr)
    except PrometheusError as exc:
        # Named as an error, never as an empty result. An error is not a value of
        # zero: a run once read six empty results as a dead fleet and wrote its
        # whole report from memory.
        return f"ERROR: query failed: {exc}\nexpr: {expr}"
    except ValueError as exc:
        return f"ERROR: {exc}"

    if not series:
        return (
            f"No series matched. This is an empty result, not an error and not zero.\n"
            f"expr: {expr}"
        )

    rows = []
    for item in series[:_MAX_SERIES]:
        metric = item.get("metric") or {}
        labels = ",".join(
            f"{key}={value}" for key, value in sorted(metric.items()) if key != "__name__"
        )
        value = item.get("value") or [None, None]
        rows.append([metric.get("__name__", ""), labels or "-", str(value[1])])

    header = [f"{len(series)} series for: {expr}"]
    if len(series) > _MAX_SERIES:
        header.append(f"(showing the first {_MAX_SERIES}; narrow the query with a label selector)")
    lines = header + render.table(["metric", "labels", "value"], rows)
    return truncate_lines(lines, BUDGET, unit="series")


def _render_range(expr: str, window: str, series: list[dict]) -> str:
    if not series:
        return f"No series matched over {window}. Empty result, not an error.\nexpr: {expr}"
    rows = []
    for item in series[:_MAX_SERIES]:
        metric = item.get("metric") or {}
        labels = ",".join(
            f"{key}={value}" for key, value in sorted(metric.items()) if key != "__name__"
        )
        values = item.get("values") or []
        if not values:
            continue
        numbers = [float(point[1]) for point in values if len(point) == 2]
        if not numbers:
            continue
        # First, last and the extremes: a trend needs two points in time, and
        # returning every sample is how a result grows large enough to end a run.
        rows.append(
            [
                labels or "-",
                render.number(numbers[0], 2),
                render.number(numbers[-1], 2),
                render.number(min(numbers), 2),
                render.number(max(numbers), 2),
                render.number(numbers[-1] - numbers[0], 2),
            ]
        )
    lines = [f"{len(series)} series over the last {window} for: {expr}"] + render.table(
        ["labels", "first", "last", "min", "max", "change"], rows
    )
    return truncate_lines(lines, BUDGET, unit="series")


def _parse_window(window: str) -> int:
    units = {"m": 60, "h": 3600, "d": 86400}
    text = window.strip().lower()
    if len(text) < 2 or text[-1] not in units or not text[:-1].isdigit():
        raise ValueError(f"window must look like 30m, 6h or 2d (got {window!r})")
    return int(text[:-1]) * units[text[-1]]


SCHEMA = {
    "type": "object",
    "properties": {
        "expr": {
            "type": "string",
            "description": "The PromQL expression, complete. Send the whole line "
            "including any function call: increase(m[1h]), not m[1h].",
        },
        "window": {
            "type": "string",
            "description": "Optional. Set to look at a trend instead of now, e.g. "
            "6h or 2d. Returns first/last/min/max/change per series.",
        },
    },
    "required": ["expr"],
    "additionalProperties": False,
}

DESCRIPTION = """
Run a PromQL query against Prometheus and get the samples back as a table.

Use this for anything the dedicated fact tools do not cover. There is no
datasource, no time and no query-type argument: an instant query is the default
and the window is resolved by the server.

Send the expression complete, including any function call around it -
increase(m[1h]) rather than m[1h]. An empty result is reported as empty and is
never zero; a failure is reported as ERROR and is never a healthy reading.
"""
