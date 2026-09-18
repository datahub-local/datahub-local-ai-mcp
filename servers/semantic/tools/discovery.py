"""`list_metrics`, `describe_metric`, `list_dimensions`, `explain`.

Discovery answers the only question the agent needs to ask: which metric, and
which dimensions may go with it. It never returns a formula, a filter condition
or a join path - those are the compiler's, and a model that cannot see them
cannot reassemble them wrongly.

`describe_metric` names its own source table on purpose. When the model has
already read which table a metric reads, the pull toward re-deriving the number
in SQL is weaker - that is the §6.2 routing rule supported by a value rather
than by a prohibition.
"""

from __future__ import annotations

from mcp_runner.budget import truncate_lines
from mcp_runner.config import tool_budget

from .. import settings
from ..compiler import compile_query, inline_sql
from ..query import DEFAULT_LIMIT, MAX_LIMIT, OPS, QueryError, parse, validate
from ..registry import GRAINS

LIST_BUDGET = tool_budget("list_metrics")
DESCRIBE_BUDGET = tool_budget("describe_metric")
DIMENSIONS_BUDGET = tool_budget("list_dimensions")
EXPLAIN_BUDGET = tool_budget("explain")

LIST_DESCRIPTION = """
Every metric this server can compute, with what each one leaves out. Start here:
a metric name from this list is the only thing `query` accepts.
Optional `search` filters by substring over name, label and description.
"""

DESCRIBE_DESCRIPTION = """
One metric in full: what it means, what it excludes, which dimensions it can be
grouped or filtered by, its finest allowed time grain, and the table it reads.
Read this before querying a metric you have not used in this conversation.
"""

DIMENSIONS_DESCRIPTION = """
The dimensions available for one metric, with sample values where known. Use it
to get filter values exactly right: a filter must match a stored value
character for character, including its case, or it matches no rows at all.
Copy a value from this tool rather than typing one.
"""

EXPLAIN_DESCRIPTION = """
Compile a semantic query to SQL and return it WITHOUT running it. Same arguments
as `query`. Use it to check a query you are unsure about; it costs no warehouse
time and returns the resolved date window.
"""

LIST_SCHEMA = {
    "type": "object",
    "properties": {"search": {"type": "string", "description": "substring filter, optional"}},
    "additionalProperties": False,
}

DESCRIBE_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string", "description": "metric name from list_metrics"}},
    "required": ["name"],
    "additionalProperties": False,
}

DIMENSIONS_SCHEMA = {
    "type": "object",
    "properties": {"metric": {"type": "string", "description": "metric name from list_metrics"}},
    "required": ["metric"],
    "additionalProperties": False,
}

QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "metrics": {
            "type": "array",
            "items": {"type": "string"},
            "description": "metric names from list_metrics, at most 5, all from one model",
        },
        "time_range": {
            "type": "object",
            "properties": {
                "grain": {"type": "string", "enum": list(GRAINS)},
                "last": {"type": "string", "description": 'e.g. "6 months", "30 days"'},
                "start": {"type": "string", "description": "YYYY-MM-DD"},
                "end": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
            },
            "required": ["grain"],
            "additionalProperties": False,
        },
        "group_by": {
            "type": "array",
            "items": {"type": "string"},
            "description": "dimension names from list_dimensions",
        },
        "filters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dimension": {"type": "string"},
                    "op": {"type": "string", "enum": list(OPS)},
                    "value": {"description": "string, number, or list for in/not_in"},
                },
                "required": ["dimension", "op", "value"],
                "additionalProperties": False,
            },
        },
        "order_by": {"type": "string", "description": "a projected name: period, a group_by, or a metric"},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
    },
    "required": ["metrics", "time_range"],
    "additionalProperties": False,
}


def list_metrics(search: str | None = None) -> str:
    registry = settings.registry()
    needle = (search or "").strip().lower()

    matches = [
        metric
        for metric in sorted(registry.metrics.values(), key=lambda m: m.name)
        if not needle
        or needle in metric.name.lower()
        or needle in metric.label.lower()
        or needle in metric.description.lower()
    ]
    if not matches:
        available = ", ".join(sorted(registry.metrics))
        return f"no metric matches {search!r}. All {len(registry.metrics)} metrics: {available}"

    lines = [f"{len(matches)} metric(s), registry {registry.version}:", ""]
    for metric in matches:
        model = registry.model_for(metric)
        lines.append(f"{metric.name} - {metric.label} [{metric.type}, min grain {metric.grain_min}]")
        lines.append(f"  means: {_one_line(metric.description)}")
        lines.append(f"  excludes: {_one_line(metric.excludes)}")
        lines.append(f"  group/filter by: {', '.join(sorted(model.dimensions))}")
        lines.append("")
    return truncate_lines(lines, LIST_BUDGET, unit="lines")


def describe_metric(name: str) -> str:
    registry = settings.registry()
    try:
        metric = registry.metric(name)
    except KeyError:
        return _no_metric(name, registry)

    model = registry.model_for(metric)
    time_dimension = model.dimensions[model.time_dimension]
    categorical = sorted(
        dimension.name for dimension in model.dimensions.values() if not dimension.is_time
    )

    lines = [
        f"{metric.name} - {metric.label}",
        f"registry: {registry.version}   owner: {metric.owner}",
        "",
        f"means: {_one_line(metric.description)}",
        "",
        f"EXCLUDES: {_one_line(metric.excludes)}",
        "",
        f"type: {metric.type}",
        f"finest time grain: {metric.grain_min}",
        f"reads: {model.table} ({_one_line(model.description)})",
        f"time dimension: {time_dimension.name} (by {time_dimension.granularity or 'day'})",
        f"group or filter by: {', '.join(categorical) or 'none'}",
        "",
        f"example: {_example(metric, categorical)}",
    ]
    return truncate_lines(lines, DESCRIBE_BUDGET, unit="lines")


def list_dimensions(metric: str) -> str:
    registry = settings.registry()
    try:
        found = registry.metric(metric)
    except KeyError:
        return _no_metric(metric, registry)

    model = registry.model_for(found)
    samples = settings.samples(model.name)

    lines = [f"dimensions for {found.name} (model {model.name}):", ""]
    for dimension in sorted(model.dimensions.values(), key=lambda d: (not d.is_time, d.name)):
        if dimension.is_time:
            lines.append(f"{dimension.name} [time, by {dimension.granularity or 'day'}]")
            lines.append("  filter with time_range, not with filters")
            continue
        detail = samples.get(dimension.name)
        lines.append(f"{dimension.name} [{dimension.type}]")
        if detail is None:
            # The warehouse could not be read and nothing was cached, so this
            # degrades to names-only rather than failing.
            lines.append("  sample values unavailable")
        else:
            # `values` is the full match set, which for a high-cardinality
            # dimension is hundreds of names, so the slice is what reaches the
            # prompt while the whole set stays available for matching.
            available = detail.get("values") or []
            cardinality = detail.get("cardinality", "?")
            shown = ", ".join(available[:8]) or "none seen"
            more = " (and more)" if isinstance(cardinality, int) and cardinality > 8 else ""
            lines.append(f"  {cardinality} distinct; e.g. {shown}{more}")
    lines.append("")
    # Says "as shown above" rather than naming a convention, because two
    # dimensions on the same model routinely disagree - one UPPERCASE from a
    # transformation, another in whatever case it was written. A blanket
    # "everything is uppercase" line is wrong for one of them and causes the
    # exact zero-match filter it means to prevent.
    lines.append(
        "Filter values must match EXACTLY as shown above, including case. "
        "Copy a value from this list rather than typing one."
    )
    return truncate_lines(lines, DIMENSIONS_BUDGET, unit="lines")


def explain(**payload) -> str:
    registry = settings.registry()
    try:
        query = parse(payload)
        validate(query, registry, settings.sample_values(model_of(payload, registry)))
        compiled = compile_query(query, registry)
    except QueryError as exc:
        return f"INVALID: {exc}"
    except KeyError as exc:
        return f"INVALID: {exc}"

    start, end = compiled.window
    lines = [
        f"registry {registry.version}, not executed",
        "",
        f"window: {start.isoformat()} to {end.isoformat()} (end exclusive), by {compiled.grain}",
        f"columns: {', '.join(compiled.projection)}",
    ]
    if compiled.partial_bucket:
        lines.append(f"partial period: {compiled.partial_bucket} is still filling")
    lines += ["", "SQL:", inline_sql(compiled)]
    return truncate_lines(lines, EXPLAIN_BUDGET, unit="lines")


def model_of(payload: dict, registry) -> str:
    names = payload.get("metrics") or []
    first = names[0] if isinstance(names, list) and names else names
    metric = registry.metrics.get(first) if isinstance(first, str) else None
    return metric.model if metric else ""


def _no_metric(name: str, registry) -> str:
    import difflib

    near = difflib.get_close_matches(name, list(registry.metrics), n=1, cutoff=0.6)
    if near:
        return f"no metric named {name!r} - did you mean {near[0]!r}?"
    return f"no metric named {name!r}. Available: {', '.join(sorted(registry.metrics))}"


def _example(metric, categorical: list[str]) -> str:
    grain = metric.grain_min
    if categorical:
        return (
            f'{{"metrics": ["{metric.name}"], "time_range": {{"grain": "{grain}", '
            f'"last": "6 {grain}s"}}, "group_by": ["{categorical[0]}"]}}'
        )
    return f'{{"metrics": ["{metric.name}"], "time_range": {{"grain": "{grain}", "last": "6 {grain}s"}}}}'


def _one_line(text: str) -> str:
    return " ".join(text.split())
