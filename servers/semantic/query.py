"""`SemanticQuery`: what an agent may ask for, and nothing else.

The type is the boundary. An undeclared filter or metric is a validation error
returned as readable text, not a wrong number - the agent never holds a string
that reaches SQL. Values from a request are bound as parameters; identifiers
from a request (`order_by`) are allowlisted against the projection.

Near-miss suggestions matter more than they look, and case is not the only
reason. A column normalised by a transformation (`trim(upper(...))`, say) only
matches an upper-cased filter, while a derived column on the same model may
hold whatever case it was written in - so no single casing rule is correct even
within one query. The suggestion is what turns a silent empty result into a
corrected retry; a rule in a prompt could not.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from mcp_runner.config import tool_cap

from .registry import GRAINS, Registry, grain_at_least

OPS = ("=", "!=", "in", "not_in", ">", ">=", "<", "<=", "contains")
_SET_OPS = frozenset({"in", "not_in"})
_ORDERED_OPS = frozenset({">", ">=", "<", "<="})

MAX_METRICS = tool_cap("metrics")
MAX_LIMIT = tool_cap("limit")
DEFAULT_LIMIT = tool_cap("default_limit")


class QueryError(Exception):
    """A query that must not compile. The message is written for the model to act on."""


@dataclass(frozen=True)
class Filter:
    dimension: str
    op: str
    value: Any


@dataclass(frozen=True)
class TimeRange:
    grain: str
    last: str | None = None
    start: date | None = None
    end: date | None = None


@dataclass(frozen=True)
class SemanticQuery:
    metrics: tuple[str, ...]
    time_range: TimeRange
    group_by: tuple[str, ...] = ()
    filters: tuple[Filter, ...] = ()
    order_by: str | None = None
    limit: int = DEFAULT_LIMIT


def parse(payload: dict[str, Any]) -> SemanticQuery:
    """Build a `SemanticQuery` from tool arguments. Shape errors only; see `validate`."""
    if not isinstance(payload, dict):
        raise QueryError("query must be an object")

    metrics = _as_list(payload.get("metrics"), "metrics")
    if not metrics:
        raise QueryError("metrics: name at least one metric")
    if len(metrics) > MAX_METRICS:
        raise QueryError(f"metrics: at most {MAX_METRICS} per query, got {len(metrics)}")

    raw_range = payload.get("time_range")
    if not isinstance(raw_range, dict):
        raise QueryError("time_range is required, e.g. {\"grain\": \"month\", \"last\": \"6 months\"}")

    grain = raw_range.get("grain")
    if grain not in GRAINS:
        raise QueryError(f"time_range.grain must be one of {', '.join(GRAINS)}, got {grain!r}")

    time_range = TimeRange(
        grain=grain,
        last=raw_range.get("last"),
        start=_as_date(raw_range.get("start"), "time_range.start"),
        end=_as_date(raw_range.get("end"), "time_range.end"),
    )
    if not time_range.last and not (time_range.start or time_range.end):
        raise QueryError(
            "time_range needs `last` (e.g. \"6 months\") or `start`/`end` (YYYY-MM-DD)"
        )

    filters = []
    for item in payload.get("filters") or []:
        if not isinstance(item, dict):
            raise QueryError("each filter must be an object with dimension, op and value")
        op = item.get("op", "=")
        if op not in OPS:
            raise QueryError(f"filter op {op!r} is not one of {', '.join(OPS)}")
        if "dimension" not in item:
            raise QueryError("each filter needs a `dimension`")
        if "value" not in item:
            raise QueryError(f"filter on {item['dimension']!r} needs a `value`")
        filters.append(Filter(dimension=item["dimension"], op=op, value=item["value"]))

    limit = payload.get("limit", DEFAULT_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise QueryError(f"limit must be a whole number, got {limit!r}")
    if not 1 <= limit <= MAX_LIMIT:
        raise QueryError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}")

    return SemanticQuery(
        metrics=tuple(metrics),
        time_range=time_range,
        group_by=tuple(_as_list(payload.get("group_by"), "group_by")),
        filters=tuple(filters),
        order_by=payload.get("order_by"),
        limit=limit,
    )


def _as_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise QueryError(f"{where} must be a string or a list of strings")


def _as_date(value: Any, where: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        # A calendar date carries no timezone; attaching one would shift the
        # window by a day for any caller east or west of UTC.
        return datetime.strptime(str(value), "%Y-%m-%d").date()  # noqa: DTZ007
    except ValueError:
        raise QueryError(f"{where} must be YYYY-MM-DD, got {value!r}") from None


def validate(query: SemanticQuery, registry: Registry, samples: dict[str, list[str]] | None = None) -> None:
    """Reject anything the registry does not declare. Raises `QueryError`."""
    for name in query.metrics:
        if name not in registry.metrics:
            raise QueryError(_unknown(name, registry.metrics, "metric"))

    metrics = [registry.metric(name) for name in query.metrics]

    models = {metric.model for metric in metrics}
    if len(models) > 1:
        raise QueryError(
            f"metrics {', '.join(query.metrics)} span models {', '.join(sorted(models))} and no "
            f"join is declared. Ask for them in separate queries."
        )

    metric = metrics[0]
    dimensions = registry.dimensions_for(metric)
    model = registry.model_for(metric)

    for name in query.group_by:
        if name not in dimensions:
            raise QueryError(_unknown(name, dimensions, f"dimension on {model.name}"))

    for item in query.filters:
        if item.dimension not in dimensions:
            raise QueryError(_unknown(item.dimension, dimensions, f"dimension on {model.name}"))
        _validate_filter_value(item, dimensions[item.dimension], samples or {})

    floor = max(metrics, key=lambda m: GRAINS.index(m.grain_min))
    if not grain_at_least(query.time_range.grain, floor.grain_min):
        raise QueryError(
            f"time_range.grain {query.time_range.grain!r} is finer than {floor.name!r} allows "
            f"(grain_min {floor.grain_min!r}). {floor.excludes.split('.')[0].strip()}."
        )

    if query.order_by is not None and query.order_by not in _projection(query):
        raise QueryError(
            f"order_by {query.order_by!r} is not a projected column. "
            f"Use one of: {', '.join(_projection(query))}"
        )


def _validate_filter_value(item: Filter, dimension, samples: dict[str, list[str]]) -> None:
    if item.op in _SET_OPS:
        if not isinstance(item.value, list) or not item.value:
            raise QueryError(f"filter {item.dimension!r} with op {item.op!r} needs a non-empty list")
        values = item.value
    else:
        if isinstance(item.value, list):
            raise QueryError(f"filter {item.dimension!r} with op {item.op!r} takes a single value")
        values = [item.value]

    if item.op in _ORDERED_OPS and dimension.type == "categorical":
        raise QueryError(
            f"filter {item.dimension!r} is categorical; op {item.op!r} needs an ordered dimension"
        )

    known = samples.get(item.dimension)
    if not known or item.op == "contains":
        return
    folded = {entry.casefold(): entry for entry in known}
    for value in values:
        if not isinstance(value, str) or value in known:
            continue
        # Case-folded throughout, for exact hits and for fuzzy ones alike.
        # Dimensions on one model routinely disagree on case, so upper() is not
        # the rule; and comparing a raw typo against upper-cased values drops a
        # real near-miss ("Mercadonna" against "MERCADONA") below the cutoff on
        # casing alone.
        exact = folded.get(value.casefold())
        if exact:
            near = [exact]
        else:
            matches = difflib.get_close_matches(value.casefold(), list(folded), n=1, cutoff=0.6)
            near = [folded[matches[0]]] if matches else []
        hint = f' - did you mean "{near[0]}"?' if near else (
            f" Known values include: {', '.join(known[:5])}."
        )
        raise QueryError(f'no {item.dimension} matches "{value}"{hint}')


def _projection(query: SemanticQuery) -> list[str]:
    return ["period", *query.group_by, *query.metrics]


def _unknown(name: str, known, kind: str) -> str:
    near = difflib.get_close_matches(name, list(known), n=1, cutoff=0.6)
    if near:
        return f"no {kind} named {name!r} - did you mean {near[0]!r}?"
    return f"no {kind} named {name!r}. Available: {', '.join(sorted(known))}"


# -- time windows ------------------------------------------------------------

_UNITS = {
    "day": "days", "days": "days",
    "week": "weeks", "weeks": "weeks",
    "month": "months", "months": "months",
    "quarter": "quarters", "quarters": "quarters",
    "year": "years", "years": "years",
}


def resolve_window(time_range: TimeRange, today: date | None = None) -> tuple[date, date]:
    """The half-open `[start, end)` window a time_range means.

    Half-open on purpose: a closed upper bound either drops the last day or
    double-counts a boundary row depending on whether the column is a date or a
    timestamp, and one model routinely carries both.
    """
    today = today or datetime.now(UTC).date()

    if time_range.start or time_range.end:
        start = time_range.start or date(1970, 1, 1)
        end = (time_range.end + timedelta(days=1)) if time_range.end else today + timedelta(days=1)
        if start >= end:
            raise QueryError(f"time_range.start {start} is not before end {time_range.end}")
        return start, end

    count, unit = _parse_last(time_range.last)
    end = today + timedelta(days=1)
    return _subtract(today, count, unit), end


def _parse_last(text: str | None) -> tuple[int, str]:
    parts = (text or "").strip().split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1].lower() in _UNITS:
        return int(parts[0]), _UNITS[parts[1].lower()]
    if len(parts) == 1 and parts[0].lower() in _UNITS:
        return 1, _UNITS[parts[0].lower()]
    raise QueryError(
        f'time_range.last {text!r} is not understood. Use "<n> <unit>", e.g. "6 months".'
    )


def _subtract(today: date, count: int, unit: str) -> date:
    """The start of a trailing window of ``count`` ``unit``s ending today.

    "1 month" is the month *up to* today, not the calendar month today falls in
    - it resolved to a single day before this was fixed, which silently answered
    "how much did I spend on dairy last month" with one day of data.
    """
    if unit == "days":
        return today - timedelta(days=count - 1)
    if unit == "weeks":
        return today - timedelta(weeks=count) + timedelta(days=1)
    months = {"months": count, "quarters": count * 3, "years": count * 12}[unit]
    year, month = today.year, today.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(today.day, _days_in(year, month))
    return date(year, month, day) + timedelta(days=1)


def _days_in(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year + (month // 12), (month % 12) + 1, 1) - timedelta(days=1)).day
